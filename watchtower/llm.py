"""Thin Anthropic wrapper: JSON answers, retries, token/cost accounting per run."""
from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass

import anthropic

from .config import settings
from .fetcher import PortalError

log = logging.getLogger("watchtower.llm")


@dataclass
class Usage:
    calls: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0

    def as_dict(self) -> dict:
        return {"calls": self.calls, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "cost_usd": self.cost_usd}


def has_key() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


_client: anthropic.AsyncAnthropic | None = None


def client() -> anthropic.AsyncAnthropic:
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(max_retries=3, timeout=300.0)
    return _client


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


class LLM:
    def __init__(self, usage: Usage | None = None, model: str | None = None):
        self.usage = usage or Usage()
        self.model = model or settings.model

    def _account(self, resp) -> None:
        u = resp.usage
        inp = (u.input_tokens or 0) + (getattr(u, "cache_creation_input_tokens", 0) or 0)
        cached = getattr(u, "cache_read_input_tokens", 0) or 0
        self.usage.calls += 1
        self.usage.input_tokens += inp + cached
        self.usage.output_tokens += u.output_tokens or 0
        self.usage.cost_usd += (inp * settings.price_in + cached * settings.price_in * 0.1
                                + (u.output_tokens or 0) * settings.price_out) / 1e6

    async def _create(self, **kw):
        c = client()
        try:
            # server-side refusal fallback (beta); fall back to the plain endpoint if unsupported
            return await c.beta.messages.create(betas=["server-side-fallback-2026-07-01"],
                                                fallbacks="default", **kw)
        except (TypeError, anthropic.BadRequestError) as e:
            log.debug("fallback-less request (%s)", e)
            return await c.messages.create(**kw)

    async def ask_json(self, system: str, user: str, *, effort: str = "medium",
                       max_tokens: int = 16000, validate=None, retries: int = 1):
        """Ask for a JSON object. `validate(obj)` may raise ValueError → one corrective retry."""
        if not has_key():
            raise PortalError("llm", "No model key configured (set ANTHROPIC_API_KEY)")
        messages = [{"role": "user", "content": user}]
        last_err = None
        for attempt in range(retries + 1):
            try:
                resp = await self._create(
                    model=self.model, max_tokens=max_tokens,
                    system=[{"type": "text", "text": system,
                             "cache_control": {"type": "ephemeral"}}],
                    output_config={"effort": effort}, messages=messages)
            except anthropic.RateLimitError as e:
                raise PortalError("llm", f"Model rate limited: {e}") from e
            except anthropic.APIStatusError as e:
                raise PortalError("llm", f"Model API error {e.status_code}: {e.message}") from e
            except anthropic.APIConnectionError as e:
                raise PortalError("llm", f"Model API unreachable: {e}") from e
            self._account(resp)
            if resp.stop_reason == "refusal":
                raise PortalError("llm", "Model declined the request")
            text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
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
