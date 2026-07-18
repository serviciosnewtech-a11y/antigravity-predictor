#!/usr/bin/env python3
"""
signal_agent/main.py — Hermes Signal Agent

Polls the Antigravity Predictor REST API. When an asset's model probability
crosses the confidence threshold (and the signal is non-NEUTRAL), fetches
recent market news via DuckDuckGo and synthesises an enriched signal brief
via the Claude API. Posts the result back to the Predictor's
POST /api/enriched-signal/{asset} endpoint, making it available on the
dashboard.

Designed to run as a persistent systemd service alongside predictor.service.

Usage:
    python3 -m signal_agent.main
    # or via systemd: see deploy/signal_agent.service

Environment variables (required):
    ANTHROPIC_API_KEY   — Anthropic API key for Claude synthesis

Optional env overrides:
    PREDICTOR_URL           — default: http://127.0.0.1:18910
    SA_CONFIDENCE_THRESHOLD — default: 0.65
    SA_COOLDOWN_SECONDS     — default: 900
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests
from loguru import logger

from .config import SignalAgentConfig, load_config
from .enricher import enrich

# ── Logging ───────────────────────────────────────────────────────────────────
logger.remove()
logger.add(sys.stderr, level="INFO",
           format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}")


# ── Predictor API client ──────────────────────────────────────────────────────

def _get_status(cfg: SignalAgentConfig) -> dict | None:
    """Fetch /api/status — returns all-asset summary dict."""
    try:
        r = requests.get(f"{cfg.predictor_url}/api/status", timeout=10)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Predictor status fetch failed: {e}")
        return None


def _get_asset_snapshot(asset: str, cfg: SignalAgentConfig) -> dict | None:
    """Fetch /api/status?symbol=<asset> — returns single-asset snapshot."""
    try:
        r = requests.get(
            f"{cfg.predictor_url}/api/status",
            params={"symbol": asset},
            timeout=10,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        logger.warning(f"Predictor snapshot fetch failed for {asset}: {e}")
        return None


def _post_enriched_signal(asset: str, payload: dict, cfg: SignalAgentConfig) -> bool:
    """POST enriched signal to the Predictor. Returns True on success."""
    # URL-safe asset key: BTC/USDT → BTC_USDT
    asset_key = asset.replace("/", "_")
    try:
        r = requests.post(
            f"{cfg.predictor_url}/api/enriched-signal/{asset_key}",
            json=payload,
            timeout=15,
        )
        r.raise_for_status()
        logger.success(f"Posted enriched signal for {asset}: {payload.get('signal')} [{payload.get('confidence_label')}]")
        return True
    except Exception as e:
        logger.error(f"Failed to post enriched signal for {asset}: {e}")
        return False


# ── Signal evaluation ─────────────────────────────────────────────────────────

def _should_enrich(asset: str, asset_status: dict, cfg: SignalAgentConfig,
                   last_enriched: dict[str, float]) -> bool:
    """
    Returns True if the asset warrants enrichment right now.

    Conditions (ALL must hold):
      1. Model signal is not NEUTRAL.
      2. At least one probability > confidence_threshold.
      3. Cooldown since last enrichment has elapsed.
    """
    signal   = asset_status.get("latest_signal", "NEUTRAL")
    long_p   = asset_status.get("latest_prediction_long",  0.0)
    short_p  = asset_status.get("latest_prediction_short", 0.0)
    max_prob = max(long_p, short_p)

    if signal == "NEUTRAL":
        return False
    if max_prob < cfg.confidence_threshold:
        return False

    since_last = time.monotonic() - last_enriched.get(asset, 0.0)
    if since_last < cfg.cooldown_seconds:
        logger.debug(
            f"[{asset}] Skipping enrichment — cooldown ({since_last:.0f}s / {cfg.cooldown_seconds}s)"
        )
        return False

    return True


# ── Main loop ─────────────────────────────────────────────────────────────────

def run(cfg: SignalAgentConfig) -> None:
    logger.info("Hermes Signal Agent starting…")
    logger.info(f"  Predictor:  {cfg.predictor_url}")
    logger.info(f"  Assets:     {cfg.assets}")
    logger.info(f"  Threshold:  {cfg.confidence_threshold}")
    logger.info(f"  Cooldown:   {cfg.cooldown_seconds}s")
    logger.info(f"  Poll every: {cfg.poll_interval_seconds}s")
    logger.info(f"  Model:      {cfg.claude_model}")

    if not cfg.anthropic_api_key:
        logger.critical("ANTHROPIC_API_KEY is not set. Agent will run but Claude synthesis will fail.")

    last_enriched: dict[str, float] = {}  # asset → monotonic timestamp of last enrichment

    while True:
        try:
            _tick(cfg, last_enriched)
        except KeyboardInterrupt:
            logger.info("Signal agent stopped.")
            break
        except Exception as e:
            logger.error(f"Unexpected error in main loop: {e}")

        time.sleep(cfg.poll_interval_seconds)


def _tick(cfg: SignalAgentConfig, last_enriched: dict[str, float]) -> None:
    status = _get_status(cfg)
    if status is None:
        logger.warning("Predictor unreachable — will retry next cycle.")
        return

    assets_status: dict[str, dict] = status.get("assets", {})
    if not assets_status:
        # Single-asset response shape (if symbol query was used)
        logger.debug("No 'assets' key in status response — unexpected shape.")
        return

    for asset in cfg.assets:
        asset_st = assets_status.get(asset)
        if asset_st is None:
            continue

        if not _should_enrich(asset, asset_st, cfg, last_enriched):
            continue

        # Fetch full snapshot (includes candles/stats) for richer enrichment context
        snapshot = _get_asset_snapshot(asset, cfg) or asset_st

        logger.info(
            f"[{asset}] Enrichment triggered — signal={asset_st.get('latest_signal')} "
            f"long={asset_st.get('latest_prediction_long', 0):.4f} "
            f"short={asset_st.get('latest_prediction_short', 0):.4f}"
        )

        payload = enrich(asset, snapshot, cfg)
        posted  = _post_enriched_signal(asset, payload, cfg)

        if posted:
            last_enriched[asset] = time.monotonic()


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    cfg = load_config()
    run(cfg)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
