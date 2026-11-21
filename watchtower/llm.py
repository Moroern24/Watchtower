"""Model access for learn / verify / repair. Provider-agnostic:

* Anthropic (Claude) through the official SDK, and
* any OpenAI-compatible chat API (OpenAI, Google Gemini, Groq, OpenRouter, DeepSeek, Mistral,
  a local Ollama, …) through plain HTTP — one adapter covers them all.

The provider is picked from environment variables (see README → "Model provider").
Every call returns a JSON object and is accounted in tokens and USD per run.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from dataclasses import dataclass

import httpx

from .config import settings
from .fetcher import PortalError

log = logging.getLogger("watchtower.llm")

# name, key variable, OpenAI-compatible base URL (None = Anthropic SDK)
PRESETS = [
    ("anthropic", "ANTHROPIC_API_KEY", None),
    ("openai", "OPENAI_API_KEY", "https://api.openai.com/v1"),
    ("gemini", "GEMINI_API_KEY", "https://generativelanguage.googleapis.com/v1beta/openai"),
    ("groq", "GROQ_API_KEY", "https://api.groq.com/openai/v1"),
    ("openrouter", "OPENROUTER_API_KEY", "https://openrouter.ai/api/v1"),
    ("deepseek", "DEEPSEEK_API_KEY", "https://api.deepseek.com/v1"),
    ("mistral", "MISTRAL_API_KEY", "https://api.mistral.ai/v1"),
]
ANTHROPIC_DEFAULT_MODEL = "claude-opus-5-5"
_sleep = asyncio.sleep  # indirection so tests can skip back-off waits


@dataclass
class Provider:
    name: str
    kind: str              # "anthropic" | "openai"
    base_url: str | None
    api_key: str | None
    model: str | None
    price_in: float        # USD per million input tokens (cost display only)
    price_out: float

    @property
    def ready(self) -> bool:
        return bool(self.model) and (bool(self.api_key) or
                                     self.name == "custom" and bool(self.base_url))

    @property
    def problem(self) -> str | None:
        if not self.model:
            return (f"{self.name}: set LLM_MODEL to a model name from your provider "
                    "(e.g. the one shown in its API docs)")
        if not self.ready:
            return f"{self.name}: no API key"
        return None


def _env(*names: str) -> str | None:
    for n in names:
        v = os.environ.get(n)
        if v and v.strip():
            return v.strip()
    return None


def _price(name: str, default: float) -> float:
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return default


def provider() -> Provider | None:
    """Resolve the configured model provider (re-read on every call; cheap)."""
    model = _env("LLM_MODEL", "WATCHTOWER_MODEL")
    forced = (_env("LLM_PROVIDER") or "").lower()
    base = _env("LLM_BASE_URL")
    if base and forced in ("", "custom", "openai-compatible"):
        # explicit OpenAI-compatible endpoint, e.g. Ollama http://localhost:11434/v1
        return Provider("custom", "openai", base.rstrip("/"), _env("LLM_API_KEY"), model,
                        _price("MODEL_PRICE_INPUT_PER_MTOK", 0.0),
                        _price("MODEL_PRICE_OUTPUT_PER_MTOK", 0.0))
    for name, key_var, url in PRESETS:
        if forced and forced != name:
            continue
        key = _env(key_var, "ANTHROPIC_AUTH_TOKEN") if name == "anthropic" else \
            _env(key_var) or (_env("LLM_API_KEY") if forced == name else None)
        if not key and forced != name:
            continue
        if name == "anthropic":
            return Provider(name, "anthropic", None, key, model or ANTHROPIC_DEFAULT_MODEL,
                            _price("MODEL_PRICE_INPUT_PER_MTOK", 4.0),
                            _price("MODEL_PRICE_OUTPUT_PER_MTOK", 20.0))
        return Provider(name, "openai", (base or url).rstrip("/"), key, model,
                        _price("MODEL_PRICE_INPUT_PER_MTOK", 0.0),
                        _price("MODEL_PRICE_OUTPUT_PER_MTOK", 0.0))
    return None


def has_key() -> bool:
    p = provider()
    return bool(p and p.ready)


def status() -> dict:
    p = provider()
    if p is None:
        return {"provider": None, "model": None, "configured": False,
                "problem": "No model key: set ANTHROPIC_API_KEY, OPENAI_API_KEY, GEMINI_API_KEY, "
                           "GROQ_API_KEY, OPENROUTER_API_KEY, … or LLM_BASE_URL (+ LLM_API_KEY)"}
    return {"provider": p.name, "model": p.model, "configured": p.ready, "problem": p.problem}


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def as_dict(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cost_usd": self.cost_usd}


def extract_json(text: str):
    m = re.search(r"```(?:json)?\s*(\{.*\}|\[.*\])\s*```", text, re.S)
    candidate = m.group(1) if m else None
    if candidate is None:
        start = min([i for i in (text.find("{"), text.find("[")) if i >= 0], default=-1)
        if start < 0:
            raise ValueError("no JSON in answer")
        end = max(text.rfind("}"), text.rfind("]"))
        candidate = text[start:end + 1]
    return json.loads(candidate)


_anthropic_client = None


def _anthropic():
    global _anthropic_client
    if _anthropic_client is None:
        import anthropic
        _anthropic_client = anthropic.AsyncAnthropic(max_retries=3, timeout=settings.llm_timeout)
    return _anthropic_client


class LLM:
    """One per run. `transport` lets tests plug in a fake OpenAI-compatible server."""

    def __init__(self, usage: Usage | None = None, prov: Provider | None = None,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.usage = usage or Usage()
        self.prov = prov
        self.transport = transport

    # ------------------------------------------------------------ public
    async def ask_json(self, system: str, user: str, *, effort: str = "medium",
                       max_tokens: int = 16000, validate=None, retries: int = 1):
        """Ask for a JSON object. `validate(obj)` may raise ValueError → one corrective retry."""
        p = self.prov or provider()
        if p is None or not p.ready:
            raise PortalError("llm", "No model configured — " + (status()["problem"] or ""))
        messages = [{"role": "user", "content": user}]
        last_err = None
        for _ in range(retries + 1):
            if p.kind == "anthropic":
                text = await self._anthropic_call(p, system, messages, effort, max_tokens)
            else:
                text = await self._openai_call(p, system, messages,
                                               min(max_tokens, settings.llm_max_output_tokens))
            try:
                obj = extract_json(text)
                if validate:
                    obj = validate(obj) or obj
                return obj
            except (ValueError, json.JSONDecodeError, PortalError) as e:
                last_err = e
                messages = messages + [
                    {"role": "assistant", "content": text or "(empty)"},
                    {"role": "user", "content": f"Your answer was not usable: {e}. "
                     "Reply again with only the corrected JSON object."}]
        raise PortalError("llm", f"Model answer unusable: {last_err}")

    def _account(self, p: Provider, inp: int, out: int, written: int = 0, cached: int = 0) -> None:
        self.usage.calls += 1
        self.usage.input_tokens += inp + written + cached
        self.usage.output_tokens += out
        self.usage.cost_usd += ((inp + written * 1.25 + cached * 0.1) * p.price_in
                                + out * p.price_out) / 1e6

    # ------------------------------------------------------------ Anthropic (SDK)
    async def _anthropic_call(self, p: Provider, system, messages, effort, max_tokens) -> str:
        import anthropic
        c = _anthropic()
        kw = dict(model=p.model, max_tokens=max_tokens, messages=messages,
                  system=[{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
                  output_config={"effort": effort})
        try:
            try:  # server-side refusal fallback (beta); plain endpoint if unsupported
                resp = await c.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                    fallbacks="default", **kw)
            except (TypeError, anthropic.BadRequestError) as e:
                log.debug("fallback-less request (%s)", e)
                resp = await c.messages.create(**kw)
        except anthropic.RateLimitError as e:
            raise PortalError("llm", f"Model rate limited: {e}") from e
        except anthropic.APIStatusError as e:
            raise PortalError("llm", f"Model API error {e.status_code}: {e.message}") from e
        except anthropic.APIConnectionError as e:
            raise PortalError("llm", f"Model API unreachable: {e}") from e
        u = resp.usage
        self._account(p, u.input_tokens or 0, u.output_tokens or 0,
                      getattr(u, "cache_creation_input_tokens", 0) or 0,
                      getattr(u, "cache_read_input_tokens", 0) or 0)
        if resp.stop_reason == "refusal":
            raise PortalError("llm", "Model declined the request")
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    # ------------------------------------------------------------ OpenAI-compatible (HTTP)
    async def _openai_call(self, p: Provider, system, messages, max_tokens: int) -> str:
        headers = {"Content-Type": "application/json"}
        if p.api_key:
            headers["Authorization"] = f"Bearer {p.api_key}"
        body = {"model": p.model, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, *messages],
                "response_format": {"type": "json_object"}}
        url = f"{p.base_url}/chat/completions"
        async with httpx.AsyncClient(timeout=settings.llm_timeout, transport=self.transport) as c:
            for attempt in range(6):
                try:
                    r = await c.post(url, json=body, headers=headers)
                except httpx.HTTPError as e:
                    if attempt < 2:
                        await _sleep(2 * (attempt + 1))
                        continue
                    raise PortalError("llm", f"Model API unreachable ({p.name}): {e}") from e
                if r.status_code == 429 or r.status_code >= 500:
                    if attempt < 5:
                        await _sleep(_retry_after(r, attempt))
                        continue
                    raise PortalError("llm", f"Model API {r.status_code} ({p.name}) after retries: "
                                             f"{_err_text(r)}")
                if r.status_code == 400 and "response_format" in body and \
                        re.search(r"response_format|json_object|json mode", r.text, re.I):
                    body.pop("response_format")       # provider/model without JSON mode
                    continue
                if r.status_code == 400 and "max_tokens" in body and \
                        re.search(r"max_completion_tokens", r.text):
                    body["max_completion_tokens"] = body.pop("max_tokens")  # reasoning models
                    continue
                if r.status_code >= 400:
                    raise PortalError("llm", f"Model API error {r.status_code} ({p.name}): "
                                             f"{_err_text(r)}")
                data = r.json()
                u = data.get("usage") or {}
                self._account(p, u.get("prompt_tokens") or 0, u.get("completion_tokens") or 0)
                choice = (data.get("choices") or [{}])[0]
                text = ((choice.get("message") or {}).get("content")) or ""
                if isinstance(text, list):  # some providers return content parts
                    text = "".join(part.get("text", "") for part in text if isinstance(part, dict))
                if not text and choice.get("finish_reason") == "length":
                    raise PortalError("llm", "Model answer cut off (raise LLM_MAX_OUTPUT_TOKENS)")
                return text
        raise PortalError("llm", f"Model API ({p.name}) did not answer")


def _retry_after(r: httpx.Response, attempt: int) -> float:
    try:
        return min(30.0, float(r.headers.get("retry-after", "")))
    except ValueError:
        return min(30.0, 2.0 * 2 ** attempt)


def _err_text(r: httpx.Response) -> str:
    try:
        j = r.json()
        err = j.get("error", j) if isinstance(j, dict) else j
        if isinstance(err, dict):
            err = err.get("message") or err
        return str(err)[:300]
    except ValueError:
        return r.text[:300]
