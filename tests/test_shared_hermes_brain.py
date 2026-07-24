"""
tests/test_shared_hermes_brain.py — regression test for merging the
dashboard chat (predictor_server.py's /api/chat) and the automated
signal-triggered enrichment (signal_agent/enricher.py) into one Hermes
brain, 2026-07-23: one backend-resolution module (llm_backend.py), one
identity core + memory file (hermes_persona.py), used by both.

Also guards the actual "client doesn't speak JSON" requirement: the
enrichment's structured JSON output must never leak into the shared memory
file that also gets recalled into the interactive chat's prompt.
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

pytest.importorskip("lightgbm")  # predictor_server imports it at module scope

import hermes_persona  # noqa: E402
import predictor_server as ps  # noqa: E402


def test_predictor_server_delegates_to_shared_llm_backend():
    # predictor_server.py's own _backend_config/_call_llm_backend are thin
    # aliases now -- confirm they're actually the shared module, not a
    # second independent implementation that could drift again.
    assert ps.llm_backend is sys.modules["llm_backend"]
    assert ps._backend_config() == ps.llm_backend.backend_config()


def test_predictor_server_operator_prompt_starts_with_shared_identity():
    assert ps._CRYPTO_OPERATOR_SYSTEM_PROMPT.startswith(hermes_persona.HERMES_CORE_IDENTITY)


def test_operator_memory_helpers_use_shared_module(tmp_path, monkeypatch):
    fake_path = tmp_path / "shared_memory.jsonl"
    monkeypatch.setattr(hermes_persona, "MEMORY_PATH", fake_path)

    ps._operator_memory_append("client asked about BTC", "here's what I see")
    recalled = ps._operator_memory_recall()
    assert "client asked about BTC" in recalled
    assert "here's what I see" in recalled


def test_enrichment_digest_is_never_raw_json(tmp_path, monkeypatch):
    """The core 'client doesn't speak JSON' requirement: whatever gets
    written to shared memory after an automated enrichment must be plain
    text, never the JSON blob itself, since memory gets recalled straight
    into a prompt (and from there, potentially quoted back to the client)."""
    fake_path = tmp_path / "shared_memory.jsonl"
    monkeypatch.setattr(hermes_persona, "MEMORY_PATH", fake_path)

    enrichment = {
        "signal": "BUY",
        "confidence_label": "High",
        "analyst_note": "Model sees strong momentum aligning with news.",
        "news_summary": "ETF inflows accelerated this week.",
        "key_risks": "Fed meeting Thursday could reverse sentiment.",
    }
    hermes_persona.record_enrichment_digest("BTC/USDT", enrichment)

    raw_line = fake_path.read_text().strip()
    rec = json.loads(raw_line)  # the JSONL *storage* format is fine to parse...
    # ...but the recorded "agent" text itself must be prose, not embedded JSON.
    assert rec["agent"].startswith("Model sees strong momentum")
    assert "{" not in rec["agent"] and "}" not in rec["agent"]
    assert "signal" not in rec["agent"].lower().split()  # no literal field-name leakage
    assert "[Automated BTC/USDT signal check" in rec["user"]


def test_enrichment_digest_falls_back_when_fields_empty(tmp_path, monkeypatch):
    fake_path = tmp_path / "shared_memory.jsonl"
    monkeypatch.setattr(hermes_persona, "MEMORY_PATH", fake_path)
    hermes_persona.record_enrichment_digest("ETH/USDT", {"signal": "NEUTRAL"})
    rec = json.loads(fake_path.read_text().strip())
    assert rec["agent"] == "No further detail generated."


def test_enricher_call_hermes_brain_pops_source_before_dashboard_payload(monkeypatch, tmp_path):
    """enrich()'s wire payload (what gets POSTed to /api/enriched-signal)
    must never contain the internal _source marker -- confirmed via the
    public enrich() entry point, not just call_hermes_brain in isolation."""
    import importlib
    import signal_agent.enricher as enricher
    importlib.reload(enricher)

    fake_memory_path = tmp_path / "shared_memory.jsonl"
    monkeypatch.setattr(hermes_persona, "MEMORY_PATH", fake_memory_path)
    monkeypatch.setattr(enricher, "fetch_news", lambda asset, cfg: [])

    def fake_call_llm(system_ctx, user_content, messages_tail=None, cfg=None, max_tokens=None):
        assert "json" not in system_ctx.lower() or "Output ONLY valid JSON" in system_ctx  # sanity: task instructions still present
        return {
            "reply": json.dumps({
                "signal": "BUY", "confidence_label": "Medium",
                "model_context": "ctx", "news_summary": "news",
                "key_risks": "risk", "analyst_note": "note",
            }),
            "source": "hermes_proxy",
        }

    monkeypatch.setattr(enricher.llm_backend, "call_llm", fake_call_llm)

    from signal_agent.config import SignalAgentConfig
    cfg = SignalAgentConfig(inference_backend="enabled")
    snapshot = {"latest_prediction_long": 0.3, "latest_prediction_short": 0.1, "latest_signal": "BUY", "position": "flat", "stats": {}}

    result = enricher.enrich("BTC/USDT", snapshot, cfg)

    assert "_source" not in result
    assert result["signal"] == "BUY"
    assert fake_memory_path.exists(), "a real (non-fallback) enrichment should record a memory digest"
