"""Provider selection and the OpenAI-compatible adapter, against a fake server (no network)."""
import asyncio
import json

import httpx
import pytest

from watchtower import config, llm
from watchtower.fetcher import PortalError

KEY_VARS = ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GEMINI_API_KEY",
            "GROQ_API_KEY", "OPENROUTER_API_KEY", "DEEPSEEK_API_KEY", "MISTRAL_API_KEY",
            "LLM_API_KEY", "LLM_BASE_URL", "LLM_PROVIDER", "LLM_MODEL", "WATCHTOWER_MODEL",
            "MODEL_PRICE_INPUT_PER_MTOK", "MODEL_PRICE_OUTPUT_PER_MTOK"]


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in KEY_VARS:
        monkeypatch.delenv(v, raising=False)
    async def no_wait(*_):
        return None
    monkeypatch.setattr(llm, "_sleep", no_wait)


def test_provider_selection(monkeypatch):
    assert llm.provider() is None and not llm.has_key()
    assert "No model key" in llm.status()["problem"]

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("LLM_MODEL", "some-model")
    assert llm.status() == {"provider": "gemini", "model": "some-model", "configured": False,
                            "problem": "gemini: no API key"}
    assert not llm.has_key()
    monkeypatch.delenv("LLM_PROVIDER")
    monkeypatch.delenv("LLM_MODEL")

    monkeypatch.setenv("GEMINI_API_KEY", "g-key")
    p = llm.provider()
    assert p.name == "gemini" and "generativelanguage.googleapis.com" in p.base_url
    assert not p.ready and "LLM_MODEL" in p.problem          # model must be named explicitly
    monkeypatch.setenv("LLM_MODEL", "some-model")
    assert llm.has_key() and llm.status() == {"provider": "gemini", "model": "some-model",
                                              "configured": True, "problem": None}

    monkeypatch.setenv("ANTHROPIC_API_KEY", "a-key")          # first match wins: Anthropic
    monkeypatch.delenv("LLM_MODEL")
    assert llm.provider().name == "anthropic" and llm.provider().model == "claude-opus-5-5"
    monkeypatch.setenv("LLM_PROVIDER", "gemini")              # …unless forced
    assert llm.provider().name == "gemini"

    monkeypatch.setenv("LLM_PROVIDER", "")
    monkeypatch.setenv("LLM_BASE_URL", "http://localhost:11434/v1/")   # e.g. Ollama, no key
    monkeypatch.setenv("LLM_MODEL", "local-model")
    p = llm.provider()
    assert p.name == "custom" and p.base_url == "http://localhost:11434/v1" and p.ready


def _server(responses, seen):
    """Fake /chat/completions: pops one (status, json) per request."""
    def handler(request: httpx.Request):
        seen.append({"url": str(request.url), "auth": request.headers.get("authorization"),
                     "body": json.loads(request.content)})
        status, payload = responses.pop(0)
        return httpx.Response(status, json=payload)
    return httpx.MockTransport(handler)


def _ok(content, pt=1000, ct=200):
    return 200, {"choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": pt, "completion_tokens": ct}}


def _prov(price_in=1.0, price_out=2.0):
    return llm.Provider("groq", "openai", "https://api.example/v1", "k-123", "m-1", price_in, price_out)


def test_openai_compatible_call_and_usage():
    seen = []
    t = _server([_ok('{"action": "list"}')], seen)
    model = llm.LLM(prov=_prov(), transport=t)
    out = asyncio.run(model.ask_json("SYSTEM say JSON", "USER"))
    assert out == {"action": "list"}
    req = seen[0]
    assert req["url"] == "https://api.example/v1/chat/completions" and req["auth"] == "Bearer k-123"
    assert req["body"]["model"] == "m-1" and req["body"]["response_format"] == {"type": "json_object"}
    assert req["body"]["messages"][0] == {"role": "system", "content": "SYSTEM say JSON"}
    u = model.usage
    assert (u.calls, u.input_tokens, u.output_tokens) == (1, 1000, 200)
    assert abs(u.cost_usd - (1000 * 1.0 + 200 * 2.0) / 1e6) < 1e-12


def test_retries_rate_limit_and_drops_unsupported_json_mode():
    seen = []
    t = _server([(429, {"error": {"message": "slow down"}}),
                 (400, {"error": {"message": "response_format json_object is not supported"}}),
                 _ok('Here you go: ```json\n{"recipe": {"x": 1}}\n```')], seen)
    out = asyncio.run(llm.LLM(prov=_prov(), transport=t).ask_json("S", "U"))
    assert out == {"recipe": {"x": 1}}
    assert "response_format" in seen[1]["body"] and "response_format" not in seen[2]["body"]


def test_corrective_retry_on_bad_json_and_clear_errors():
    seen = []
    t = _server([_ok("not json at all"), _ok('{"ok": true}')], seen)
    assert asyncio.run(llm.LLM(prov=_prov(), transport=t).ask_json("S", "U")) == {"ok": True}
    assert "not usable" in seen[1]["body"]["messages"][-1]["content"]

    t = _server([(401, {"error": {"message": "Invalid API key"}})], [])
    with pytest.raises(PortalError) as e:
        asyncio.run(llm.LLM(prov=_prov(), transport=t).ask_json("S", "U"))
    assert "401" in e.value.message and "Invalid API key" in e.value.message


def test_no_provider_is_a_clear_error():
    with pytest.raises(PortalError) as e:
        asyncio.run(llm.LLM().ask_json("S", "U"))
    assert "No model configured" in e.value.message


def test_dotenv_does_not_override_real_env(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text('# comment\nGROQ_API_KEY="from-file"\nexport LLM_MODEL=file-model\n'
                                   "OPENAI_API_KEY=from-file\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "from-env")
    config.load_dotenv()
    import os
    assert os.environ["GROQ_API_KEY"] == "from-file" and os.environ["LLM_MODEL"] == "file-model"
    assert os.environ["OPENAI_API_KEY"] == "from-env"
