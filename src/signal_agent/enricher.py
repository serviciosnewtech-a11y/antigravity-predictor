"""
signal_agent/enricher.py — News fetch + Claude API synthesis.

Given a Predictor snapshot for one asset, this module:
  1. Searches for recent news about the asset and macro environment.
  2. Calls the Claude API with both the quantitative signal and the news.
  3. Returns a structured EnrichedSignal dict ready to POST to the Predictor.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from loguru import logger

from .config import SignalAgentConfig


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


# ── Claude synthesis ──────────────────────────────────────────────────────────

_SYSTEM_PROMPT = """\
You are a concise, factual crypto trading signal analyst for an advisory-only system.

Your role is to synthesize a quantitative model output with recent market news into a structured signal brief. You do NOT give financial advice. You flag uncertainty clearly. You are brief — the dashboard has limited space.

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


def call_claude(prompt: str, cfg: SignalAgentConfig) -> dict:
    """Call the Anthropic Claude API."""
    try:
        import anthropic  # type: ignore
    except ImportError:
        logger.error("anthropic package not installed. pip install anthropic")
        return _fallback_signal("anthropic package missing")

    if not cfg.anthropic_api_key:
        logger.error("ANTHROPIC_API_KEY not set — cannot synthesize signal.")
        return _fallback_signal("no API key")

    try:
        client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
        response = client.messages.create(
            model=cfg.claude_model,
            max_tokens=512,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text
        return _parse_llm_response(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Claude returned invalid JSON: {e}")
        return _fallback_signal(f"JSON parse error: {e}")
    except Exception as e:
        logger.error(f"Claude API call failed: {e}")
        return _fallback_signal(str(e))


def call_ollama(prompt: str, cfg: SignalAgentConfig) -> dict:
    """
    Call a local Ollama instance via its OpenAI-compatible /v1/chat/completions endpoint.
    No API key required — just needs Ollama running locally with the model pulled.

    Quick setup:
        ollama pull llama3.1          # or any model you prefer
        ollama serve                  # starts on http://localhost:11434
    Then set: SA_INFERENCE_BACKEND=ollama
    """
    url = f"{cfg.ollama_url.rstrip('/')}/v1/chat/completions"
    payload = {
        "model": cfg.ollama_model,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user",   "content": prompt},
        ],
        "max_tokens": 512,
        "temperature": 0.2,   # Low temperature for consistent JSON output
        "stream": False,
    }
    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        raw  = data["choices"][0]["message"]["content"]
        return _parse_llm_response(raw)
    except json.JSONDecodeError as e:
        logger.error(f"Ollama returned invalid JSON: {e}")
        return _fallback_signal(f"Ollama JSON parse error: {e}")
    except Exception as e:
        logger.error(f"Ollama call failed ({cfg.ollama_url}, model={cfg.ollama_model}): {e}")
        return _fallback_signal(f"Ollama error: {e}")


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
      3. Call Claude
      4. Return structured payload (ready to POST to /api/enriched-signal/{asset})
    """
    logger.info(f"[enricher] Fetching news for {asset}…")
    t0 = time.monotonic()
    news = fetch_news(asset, cfg)
    logger.info(f"[enricher] Got {len(news)} news items in {time.monotonic()-t0:.1f}s")

    prompt = _build_user_prompt(asset, snapshot, news)

    backend = cfg.inference_backend.lower()
    if backend == "ollama":
        logger.info(f"[enricher] Calling Ollama ({cfg.ollama_url}, model={cfg.ollama_model}) for {asset}…")
        t1 = time.monotonic()
        result = call_ollama(prompt, cfg)
        logger.info(f"[enricher] Ollama responded in {time.monotonic()-t1:.1f}s")
    else:
        logger.info(f"[enricher] Calling Claude ({cfg.claude_model}) for {asset}…")
        t1 = time.monotonic()
        result = call_claude(prompt, cfg)
        logger.info(f"[enricher] Claude responded in {time.monotonic()-t1:.1f}s")

    # Attach metadata
    result["asset"] = asset
    result["long_probability"]  = snapshot.get("latest_prediction_long",  0.0)
    result["short_probability"] = snapshot.get("latest_prediction_short", 0.0)
    result["model_signal"]      = snapshot.get("latest_signal", "NEUTRAL")
    result["news_count"]        = len(news)
    if "generated_at" not in result:
        result["generated_at"] = datetime.now(timezone.utc).isoformat()

    return result
