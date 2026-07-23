"""
signal_agent/enricher.py — News fetch + LLM signal synthesis.

Given a Predictor snapshot for one asset, this module:
  1. Searches for recent news about the asset and macro environment.
  2. Calls the configured LLM backend with both the quantitative signal and the news.
  3. Returns a structured EnrichedSignal dict ready to POST to the Predictor.

This is the SAME Hermes brain as the dashboard's interactive chat
(predictor_server.py's /api/chat) — same backend-resolution module
(llm_backend.py, CHAT_BACKEND-driven), same core identity/memory
(hermes_persona.py). It used to be a fully separate implementation (its own
call_claude/call_ollama/call_openai_compatible, its own SA_INFERENCE_BACKEND-
driven backend selection) that had already drifted from the chat's version
(no native Anthropic support here, different env vars) — merged 2026-07-23
so there's one implementation of "call an LLM backend" instead of two to
keep in sync by hand.

The dashboard's Agent Report panel needs specific fields to render (signal/
confidence_label/news_summary/key_risks/analyst_note), so the OUTPUT of
this module stays structured JSON on the wire — but that JSON is purely
internal plumbing between this process and predictor_server.py's
/api/enriched-signal/{asset} endpoint. The dashboard (dashboard/app.js)
parses each field into its own styled UI element (setText calls) and never
renders raw JSON to the client; nothing JSON-shaped gets written into
Hermes's shared memory either (see hermes_persona.record_enrichment_digest)
— the client never "speaks JSON" anywhere in this pipeline.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from .config import SignalAgentConfig

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))  # src/
import llm_backend  # noqa: E402
import hermes_persona  # noqa: E402


# ── News fetching ─────────────────────────────────────────────────────────────

# Asset display names for search queries
_ASSET_NAMES: dict[str, str] = {
    "BTC/USDT": "Bitcoin BTC",
    "ETH/USDT": "Ethereum ETH",
    "SOL/USDT": "Solana SOL",
}

_MACRO_QUERIES = [
    "Federal Reserve interest rate crypto",
    "US dollar DXY crypto market",
    "VIX volatility market risk",
]


def _search_ddg(query: str, max_results: int = 3) -> list[dict]:
    """
    Search DuckDuckGo via the duckduckgo-search package (no API key needed).
    Returns list of {title, body, href}.
    Falls back to empty list on import/network error.
    """
    try:
        from duckduckgo_search import DDGS  # type: ignore
        results = []
        with DDGS() as ddg:
            for r in ddg.news(query, max_results=max_results):
                results.append({
                    "title": r.get("title", ""),
                    "body":  r.get("body", r.get("excerpt", "")),
                    "url":   r.get("url", ""),
                    "date":  r.get("date", ""),
                })
        return results
    except ImportError:
        logger.warning("duckduckgo-search not installed — skipping news fetch. pip install duckduckgo-search")
        return []
    except Exception as e:
        logger.warning(f"DDG search failed ({query!r}): {e}")
        return []


def fetch_news(asset: str, cfg: SignalAgentConfig) -> list[dict]:
    """
    Fetch recent news for the asset and macro environment.
    Returns a deduplicated list of news dicts, capped at cfg.max_news_items.
    """
    asset_name = _ASSET_NAMES.get(asset, asset.split("/")[0])
    queries = [
        f"{asset_name} crypto news today",
        f"{asset_name} price analysis",
    ] + _MACRO_QUERIES[:2]

    seen_urls: set[str] = set()
    items: list[dict] = []
    for q in queries:
        for item in _search_ddg(q, max_results=3):
            url = item.get("url", "")
            if url and url in seen_urls:
                continue
            seen_urls.add(url)
            items.append(item)
        if len(items) >= cfg.max_news_items:
            break

    return items[:cfg.max_news_items]


# ── LLM synthesis ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = hermes_persona.HERMES_CORE_IDENTITY + """

Right now you're not chatting with the client — you've been triggered automatically because a signal
crossed its confidence threshold, and there's no one here to ask a clarifying question of. Synthesize the
quantitative model output below with recent market news into a structured signal brief for the dashboard's
Agent Report panel. Be brief — the panel has limited space. Flag uncertainty clearly rather than sounding
confident when you're not.

Output ONLY valid JSON matching this schema (no markdown, no prose outside JSON):
{
  "signal": "BUY" | "SELL" | "NEUTRAL" | "EXIT",
  "confidence_label": "High" | "Medium" | "Low",
  "model_context": "<one sentence: what the model sees>",
  "news_summary": "<2-3 sentences: key relevant news>",
  "key_risks": "<one sentence: top 1-2 risk factors right now>",
  "analyst_note": "<one sentence: how news aligns or conflicts with model signal>",
  "generated_at": "<ISO 8601 UTC timestamp>"
}

Rules:
- confidence_label: High if prob > 0.75, Medium if 0.65-0.75, Low otherwise.
- If news strongly contradicts the model signal, flag it in analyst_note.
- Never promise profits. Never mention specific price targets.
- If news is unavailable or thin, say so in news_summary.
- This JSON is read by predictor_server.py's own code, never shown to the client as JSON — each field
  gets rendered into its own place on the Agent Report panel. Every string field is still displayed as
  text, though, so write each one in plain, client-readable language.
"""


def _build_user_prompt(asset: str, snapshot: dict, news: list[dict]) -> str:
    long_prob  = snapshot.get("latest_prediction_long",  0.0)
    short_prob = snapshot.get("latest_prediction_short", 0.0)
    signal     = snapshot.get("latest_signal", "NEUTRAL")
    position   = snapshot.get("position", "flat")
    stats      = snapshot.get("stats", {})

    news_text = ""
    if news:
        news_text = "\n".join(
            f"- [{item.get('date', 'recent')}] {item['title']}: {item.get('body', '')[:200]}"
            for item in news
        )
    else:
        news_text = "No news retrieved."

    return f"""
Asset: {asset}
Current UTC time: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}

Model output:
  Signal:           {signal}
  Long probability: {long_prob:.4f}  (buy threshold: {snapshot.get('buy_threshold', 'N/A')})
  Short probability:{short_prob:.4f}  (sell threshold: {snapshot.get('sell_threshold', 'N/A')})
  Current position: {position}
  Paper sim P&L:    {stats.get('total_pnl_pct', 0.0):.2f}%  ({stats.get('total_trades', 0)} trades)

Recent market news (last {len(news)} items):
{news_text}

Generate the signal brief JSON now.
""".strip()


def _parse_llm_response(raw: str) -> dict:
    """Strip markdown fences and parse JSON from any LLM response."""
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    # Some models wrap the JSON in extra prose — find first { … }
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    return json.loads(raw)


def call_hermes_brain(prompt: str, cfg: SignalAgentConfig) -> dict:
    """
    Calls the shared Hermes brain (llm_backend.call_llm — same backend
    resolution as the dashboard chat, driven by CHAT_BACKEND/HERMES_PROXY_URL/
    ANTHROPIC_API_KEY/OLLAMA_URL, not SignalAgentConfig's own now-vestigial
    hermes_proxy_url/ollama_url/claude_model fields, which stay in config.py
    for config.json backward-compatibility but are no longer what actually
    gets called — env vars are the one source of truth for backend choice,
    same as /api/chat). Recalls shared memory first, so an automated note
    can be informed by recent chat/enrichment history the same way the chat
    side recalls it for conversations.
    """
    memory = hermes_persona.memory_recall()
    system_ctx = _SYSTEM_PROMPT
    if memory:
        system_ctx += f"\n\n[Recalled memory — recent chat/enrichment history]\n{memory}"

    result = llm_backend.call_llm(system_ctx, prompt, cfg=llm_backend.backend_config(), max_tokens=512)
    if not result:
        logger.error("No LLM backend configured/reachable for signal-agent enrichment.")
        return _fallback_signal("no LLM backend configured/reachable")

    try:
        parsed = _parse_llm_response(result["reply"])
        parsed["_source"] = result["source"]
        return parsed
    except json.JSONDecodeError as e:
        logger.error(f"{result['source']} returned invalid JSON: {e}")
        return _fallback_signal(f"JSON parse error: {e}")


def _fallback_signal(reason: str) -> dict:
    return {
        "signal": "NEUTRAL",
        "confidence_label": "Low",
        "model_context": "Signal enrichment unavailable.",
        "news_summary": "News fetch skipped.",
        "key_risks": "Enrichment error — see agent logs.",
        "analyst_note": f"Fallback: {reason}",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


# ── Public interface ──────────────────────────────────────────────────────────

def enrich(asset: str, snapshot: dict, cfg: SignalAgentConfig) -> dict:
    """
    Full enrichment pipeline for one asset:
      1. Fetch news
      2. Build prompt
      3. Call configured LLM backend
      4. Return structured payload (ready to POST to /api/enriched-signal/{asset})
    """
    logger.info(f"[enricher] Fetching news for {asset}…")
    t0 = time.monotonic()
    news = fetch_news(asset, cfg)
    logger.info(f"[enricher] Got {len(news)} news items in {time.monotonic()-t0:.1f}s")

    prompt = _build_user_prompt(asset, snapshot, news)

    # SA_INFERENCE_BACKEND is now purely an on/off switch for enrichment --
    # which backend actually answers is CHAT_BACKEND/llm_backend.py's job,
    # same as the dashboard chat. Any non-disabled value here just means
    # "enabled"; the specific values (claude/ollama/openai_compatible) that
    # used to also select a backend are accepted for backward compatibility
    # with existing .env files but no longer change what gets called.
    backend = cfg.inference_backend.lower()
    if backend in {"disabled", "none", "off"}:
        logger.info(f"[enricher] Enrichment disabled for {asset}; returning neutral fallback.")
        result = _fallback_signal("LLM enrichment disabled")
    else:
        logger.info(f"[enricher] Calling Hermes brain for {asset}…")
        t1 = time.monotonic()
        result = call_hermes_brain(prompt, cfg)
        logger.info(f"[enricher] Hermes brain responded in {time.monotonic()-t1:.1f}s (source={result.get('_source', 'fallback')})")

    # _source is an internal marker (which backend actually answered) used
    # for logging/memory-gating below — never part of the wire payload the
    # dashboard consumes, so it's popped before attaching the rest.
    had_real_reply = bool(result.pop("_source", None))

    # Attach metadata
    result["asset"] = asset
    result["long_probability"]  = snapshot.get("latest_prediction_long",  0.0)
    result["short_probability"] = snapshot.get("latest_prediction_short", 0.0)
    result["model_signal"]      = snapshot.get("latest_signal", "NEUTRAL")
    result["news_count"]        = len(news)
    if "generated_at" not in result:
        result["generated_at"] = datetime.now(timezone.utc).isoformat()

    if had_real_reply:
        hermes_persona.record_enrichment_digest(asset, result)

    return result
