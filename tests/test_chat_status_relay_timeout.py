"""
tests/test_chat_status_relay_timeout.py — regression test for a real live
bug found 2026-07-23: /api/chat/status's local-relay health probe
(_probe_local_relay(), nested inside chat_status() in src/predictor_server.py)
had a bare hardcoded `timeout=12` on its `requests.get(f"{endpoint}/health",
...)` call. The comment right next to it claimed this "matched
AGENT_RELAY_HEALTHCHECK_TIMEOUT_S's default", but nothing actually read that
env var -- it was pure hardcoded literal, completely disconnected from the
relay's own configurable healthcheck budget (tools/agent_chat_relay.py's
HEALTHCHECK_TIMEOUT_S, default 10s, itself driven by that same env var).

This bit a live deploy: an operator raised AGENT_RELAY_HEALTHCHECK_TIMEOUT_S
to 15 (to give a real CLI agent more headroom on a cache-miss health check),
but predictor_server.py's client-side wait for that same call stayed
hardcoded at 12 -- less than the relay's own configured budget. The result:
predictor gave up and reported "relay unreachable: ... Read timed out" for
a relay that was still legitimately working, just taking a few seconds
longer than predictor's stale hardcoded timeout allowed for.

Fix: read AGENT_RELAY_HEALTHCHECK_TIMEOUT_S (same env var, same default of
"10" as the relay side) and use relay_budget + 2s as the client-side
timeout, so predictor's wait always structurally exceeds whatever budget the
relay itself was actually configured with.
"""
import os
import sys
from unittest.mock import patch

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("lightgbm")  # predictor_server imports it at module scope

import predictor_server as ps  # noqa: E402


class _FakeHealthResponse:
    status_code = 200

    def json(self):
        return {"agent_binary_exists": True, "live_ok": True}


def _run_chat_status_with_local_relay(monkeypatch):
    monkeypatch.setenv("HERMES_PROXY_URL", "http://127.0.0.1:8645")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OLLAMA_URL", raising=False)
    monkeypatch.delenv("CHAT_BACKEND", raising=False)
    with patch.object(ps, "requests") as mock_requests:
        mock_requests.get.return_value = _FakeHealthResponse()
        ps.chat_status()
        return mock_requests.get


def test_relay_healthcheck_timeout_honors_env_var(monkeypatch):
    monkeypatch.setenv("AGENT_RELAY_HEALTHCHECK_TIMEOUT_S", "15")
    mock_get = _run_chat_status_with_local_relay(monkeypatch)

    assert mock_get.called, "chat_status() should probe the local relay's /health"
    _, kwargs = mock_get.call_args
    assert kwargs.get("timeout") == 17.0, (
        f"expected client-side timeout to track AGENT_RELAY_HEALTHCHECK_TIMEOUT_S=15 "
        f"(+2s buffer = 17), got {kwargs.get('timeout')!r} -- this is the exact live bug "
        f"where the timeout was a bare hardcoded 12, ignoring the env var entirely"
    )


def test_relay_healthcheck_timeout_default_matches_relay_default(monkeypatch):
    monkeypatch.delenv("AGENT_RELAY_HEALTHCHECK_TIMEOUT_S", raising=False)
    mock_get = _run_chat_status_with_local_relay(monkeypatch)

    _, kwargs = mock_get.call_args
    # Relay-side default (tools/agent_chat_relay.py) is "10" -- client-side
    # should default to that same 10 plus the 2s buffer, not an unrelated
    # hardcoded value.
    assert kwargs.get("timeout") == 12.0
