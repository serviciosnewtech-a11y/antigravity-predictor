"""
tests/test_chat_backend_swap.py — the dashboard ships pointed at Hermes by
default, but a client must be able to swap the shared Hermes brain (both
the dashboard's /api/chat AND signal_agent/enricher.py's automated
enrichment) to a different agent without touching source code. Covers:
auto-detect priority order (hermes_proxy -> anthropic -> ollama),
CHAT_BACKEND forcing exactly one backend and refusing to silently fail over
to another, and the native Anthropic Messages API call (a genuinely
different wire shape from the OpenAI-compatible proxy path: system is a
top-level param not a "system" role, auth is x-api-key not Bearer, reply is
content[0]["text"] not choices[0]["message"]["content"]).

Targets src/llm_backend.py directly — this is where the logic actually
lives (both predictor_server.py's /api/chat and signal_agent/enricher.py's
call_hermes_brain() are thin callers into this module).
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import llm_backend  # noqa: E402


class _FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self):
        return self._payload


def _clear_backend_env(monkeypatch):
    for var in ("HERMES_PROXY_URL", "ANTHROPIC_API_KEY", "OLLAMA_URL", "CHAT_BACKEND"):
        monkeypatch.delenv(var, raising=False)


def test_auto_detect_prefers_hermes_proxy_over_anthropic_and_ollama(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("HERMES_PROXY_URL", "http://fake-proxy:1234")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("OLLAMA_URL", "http://fake-ollama:1234")

    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        if "fake-proxy" in url:
            return _FakeResponse(200, {"choices": [{"message": {"content": "from hermes proxy"}}]})
        raise AssertionError(f"should not have called {url} -- hermes_proxy should win auto-detect")

    monkeypatch.setattr(llm_backend, "requests", type("R", (), {"post": staticmethod(fake_post)}))

    cfg = llm_backend.backend_config()
    result = llm_backend.call_llm("system prompt", "hello", cfg=cfg)
    assert result == {"reply": "from hermes proxy", "source": "hermes_proxy"}
    assert len(calls) == 1


def test_chat_backend_override_forces_anthropic_even_with_proxy_set(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("HERMES_PROXY_URL", "http://fake-proxy:1234")  # would win auto-detect
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")
    monkeypatch.setenv("CHAT_BACKEND", "anthropic")  # ...but this forces anthropic instead

    calls = []

    def fake_post(url, json=None, headers=None, **kwargs):
        calls.append(url)
        assert url == "https://api.anthropic.com/v1/messages"
        assert json["system"] == "system prompt"
        assert all(m["role"] != "system" for m in json["messages"])
        assert headers["x-api-key"] == "sk-fake"
        assert "Authorization" not in headers
        return _FakeResponse(200, {"content": [{"type": "text", "text": "from anthropic"}]})

    monkeypatch.setattr(llm_backend, "requests", type("R", (), {"post": staticmethod(fake_post)}))

    cfg = llm_backend.backend_config()
    result = llm_backend.call_llm("system prompt", "hello", cfg=cfg)
    assert result == {"reply": "from anthropic", "source": "anthropic"}
    assert calls == ["https://api.anthropic.com/v1/messages"]


def test_chat_backend_override_misconfigured_does_not_fall_through(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("OLLAMA_URL", "http://fake-ollama:1234")
    monkeypatch.setenv("CHAT_BACKEND", "anthropic")

    def fake_post(url, **kwargs):
        raise AssertionError(f"should not have called {url} -- anthropic key is missing, explicit override must not fall through")

    monkeypatch.setattr(llm_backend, "requests", type("R", (), {"post": staticmethod(fake_post)}))

    cfg = llm_backend.backend_config()
    result = llm_backend.call_llm("system prompt", "hello", cfg=cfg)
    assert result is None


def test_chat_backend_override_ollama_still_uses_ollama_path(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("HERMES_PROXY_URL", "http://fake-proxy:1234")
    monkeypatch.setenv("CHAT_BACKEND", "ollama")
    monkeypatch.setenv("OLLAMA_URL", "http://fake-ollama:1234")

    def fake_post(url, **kwargs):
        assert "fake-ollama" in url
        return _FakeResponse(200, {"message": {"content": "from ollama"}})

    monkeypatch.setattr(llm_backend, "requests", type("R", (), {"post": staticmethod(fake_post)}))

    cfg = llm_backend.backend_config()
    result = llm_backend.call_llm("system prompt", "hello", cfg=cfg)
    assert result == {"reply": "from ollama", "source": "ollama"}


def test_unrecognized_chat_backend_returns_none_without_calling_anything(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("HERMES_PROXY_URL", "http://fake-proxy:1234")
    monkeypatch.setenv("CHAT_BACKEND", "some_framework_nobody_wrote_yet")

    def fake_post(url, **kwargs):
        raise AssertionError("should not call any backend for an unrecognized CHAT_BACKEND value")

    monkeypatch.setattr(llm_backend, "requests", type("R", (), {"post": staticmethod(fake_post)}))

    cfg = llm_backend.backend_config()
    result = llm_backend.call_llm("system prompt", "hello", cfg=cfg)
    assert result is None


def test_anthropic_model_env_override(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_CHAT_MODEL", "claude-opus-4-8")
    cfg = llm_backend.backend_config()
    assert cfg["anthropic_model"] == "claude-opus-4-8"


def test_max_tokens_passed_through_to_anthropic(monkeypatch):
    _clear_backend_env(monkeypatch)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake")

    captured = {}

    def fake_post(url, json=None, **kwargs):
        captured["max_tokens"] = json["max_tokens"]
        return _FakeResponse(200, {"content": [{"type": "text", "text": "ok"}]})

    monkeypatch.setattr(llm_backend, "requests", type("R", (), {"post": staticmethod(fake_post)}))

    cfg = llm_backend.backend_config()
    llm_backend.call_llm("system prompt", "hello", cfg=cfg, max_tokens=512)
    assert captured["max_tokens"] == 512
