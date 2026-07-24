"""
tests/test_chat_unification.py — regression test for merging the dashboard's
two chat personas (operational "Hermes" + "Hermes Tutor") back into one,
2026-07-23. Guards against any of the old split quietly coming back: a
second endpoint, a second system prompt, or a second memory file/backend-
config prefix.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("lightgbm")  # predictor_server imports it at module scope

import predictor_server as ps  # noqa: E402


def test_tutor_chat_route_is_gone():
    paths = {getattr(r, "path", None) for r in ps.app.routes}
    assert "/api/tutor-chat" not in paths
    assert "/api/chat" in paths, "the one remaining chat endpoint should still exist"


def test_tutor_system_prompt_and_helpers_removed():
    assert not hasattr(ps, "_TUTOR_SYSTEM_PROMPT")
    assert not hasattr(ps, "_TUTOR_MEMORY_PATH")
    assert not hasattr(ps, "_tutor_memory_recall")
    assert not hasattr(ps, "_tutor_memory_append")
    assert not hasattr(ps, "hermes_tutor_chat")


def test_backend_config_takes_no_prefix_arg():
    # Used to be _backend_config(prefix) so the tutor could override to a
    # different backend/model -- now there's only one persona, so this
    # should work with zero args. Fields grew (backend_override, anthropic_*)
    # when CHAT_BACKEND/native-Anthropic support was added.
    cfg = ps._backend_config()
    assert set(cfg.keys()) == {
        "backend_override", "proxy_url", "proxy_key", "proxy_model",
        "anthropic_key", "anthropic_model", "ollama_url", "ollama_model",
    }


def test_chat_status_reports_single_surface(monkeypatch):
    monkeypatch.delenv("HERMES_PROXY_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("CHAT_BACKEND", raising=False)
    result = ps.chat_status()
    assert set(result.keys()) == {"chat"}, f"expected exactly one chat surface, got: {list(result.keys())}"
    assert result["chat"]["route"] == "/api/chat"
    assert result["chat"]["configured"] is False


def test_operator_prompt_covers_former_tutor_capabilities():
    # The merged prompt needs to still cover what the tutor used to own:
    # teaching general concepts and citing real model-performance numbers.
    prompt = ps._CRYPTO_OPERATOR_SYSTEM_PROMPT
    assert "Model performance context" in prompt
    assert "risk management" in prompt
