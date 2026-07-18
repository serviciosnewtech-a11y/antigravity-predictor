"""
signal_agent/config.py — Configuration for the Hermes signal agent.

Priority: environment variables > config.json signal_agent block > defaults.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path


# Locate the Predictor's config.json (one directory above src/)
_SRC_DIR = Path(__file__).resolve().parent.parent
_CONFIG_PATH = _SRC_DIR / "config.json"


def _load_predictor_config() -> dict:
    try:
        with open(_CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


@dataclass
class SignalAgentConfig:
    # Predictor base URL (internal — no need to go through nginx on the same host)
    predictor_url: str = "http://127.0.0.1:18910"

    # Only trigger enrichment when model probability exceeds this.
    # Calibrated to the actual LightGBM output range (0.18–0.28 for this dataset).
    # 0.65 was the original placeholder — it would never fire.
    confidence_threshold: float = 0.22

    # How often to poll the Predictor REST API (seconds)
    poll_interval_seconds: int = 30

    # Minimum seconds between two enrichment calls for the same asset
    # Prevents hammering Claude API if the model stays above threshold
    cooldown_seconds: int = 900  # 15 minutes

    # Recent news window to fetch
    news_lookback_hours: int = 6

    # Max news snippets to pass to Claude
    max_news_items: int = 6

    # Inference backend: "claude" | "ollama"
    # Set SA_INFERENCE_BACKEND=ollama to use local Ollama instead of Claude API
    inference_backend: str = "claude"

    # Claude model for synthesis (haiku = fast + cheap)
    claude_model: str = "claude-haiku-4-5-20251001"

    # Anthropic API key (prefer env var — not needed when backend=ollama)
    anthropic_api_key: str = ""

    # Ollama settings (used when inference_backend="ollama")
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"   # Any model pulled in Ollama

    # Assets to monitor (normalised symbols)
    assets: list[str] = field(default_factory=lambda: ["BTC/USDT", "ETH/USDT", "SOL/USDT"])


def load_config() -> SignalAgentConfig:
    """
    Build a SignalAgentConfig.
    1. Start with defaults.
    2. Override with `signal_agent` block from config.json.
    3. Override with environment variables.
    """
    predictor_cfg = _load_predictor_config()
    sa_block = predictor_cfg.get("signal_agent", {})

    cfg = SignalAgentConfig(
        predictor_url=sa_block.get("predictor_url", "http://127.0.0.1:18910"),
        confidence_threshold=float(sa_block.get("confidence_threshold", 0.22)),
        poll_interval_seconds=int(sa_block.get("poll_interval_seconds", 30)),
        cooldown_seconds=int(sa_block.get("cooldown_seconds", 900)),
        news_lookback_hours=int(sa_block.get("news_lookback_hours", 6)),
        max_news_items=int(sa_block.get("max_news_items", 6)),
        claude_model=sa_block.get("claude_model", "claude-haiku-4-5-20251001"),
        anthropic_api_key=sa_block.get("anthropic_api_key", ""),
        assets=list(predictor_cfg.get("assets", {}).keys()) or ["BTC/USDT", "ETH/USDT", "SOL/USDT"],
    )

    # Env overrides — always take precedence. Never hardcode keys in config.json.
    if os.environ.get("ANTHROPIC_API_KEY"):
        cfg.anthropic_api_key = os.environ["ANTHROPIC_API_KEY"]
    if os.environ.get("PREDICTOR_URL"):
        cfg.predictor_url = os.environ["PREDICTOR_URL"]
    if os.environ.get("SA_CONFIDENCE_THRESHOLD"):
        cfg.confidence_threshold = float(os.environ["SA_CONFIDENCE_THRESHOLD"])
    if os.environ.get("SA_COOLDOWN_SECONDS"):
        cfg.cooldown_seconds = int(os.environ["SA_COOLDOWN_SECONDS"])
    if os.environ.get("SA_POLL_INTERVAL"):
        cfg.poll_interval_seconds = int(os.environ["SA_POLL_INTERVAL"])
    if os.environ.get("SA_INFERENCE_BACKEND"):
        cfg.inference_backend = os.environ["SA_INFERENCE_BACKEND"]
    if os.environ.get("OLLAMA_URL"):
        cfg.ollama_url = os.environ["OLLAMA_URL"]
    if os.environ.get("OLLAMA_MODEL"):
        cfg.ollama_model = os.environ["OLLAMA_MODEL"]

    return cfg
