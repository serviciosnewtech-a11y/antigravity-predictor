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

    # Inference backend: "disabled" | "claude" | "ollama" | "openai_compatible" | "hermes_proxy"
    # Default is disabled so the client deployment works without Hermes, LLMs, or API keys.
    inference_backend: str = "disabled"

    # Claude model for synthesis (haiku = fast + cheap)
    claude_model: str = "claude-haiku-4-5-20251001"

    # Anthropic API key (prefer env var — not needed when backend=ollama or openai_compatible)
    anthropic_api_key: str = ""

    # Ollama settings (used when inference_backend="ollama")
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.1"   # Any model pulled in Ollama

    # Hermes Proxy / OpenAI Compatible settings
    hermes_proxy_url: str = "http://host.docker.internal:8645/v1"
    hermes_inference_model: str = "operator-approved-model"
    hermes_proxy_api_key: str = "local"

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
        inference_backend=sa_block.get("inference_backend", "disabled"),
        claude_model=sa_block.get("claude_model", "claude-haiku-4-5-20251001"),
        anthropic_api_key=sa_block.get("anthropic_api_key", ""),
        ollama_url=sa_block.get("ollama_url", "http://localhost:11434"),
        ollama_model=sa_block.get("ollama_model", "llama3.1"),
        hermes_proxy_url=sa_block.get("hermes_proxy_url", "http://host.docker.internal:8645/v1"),
        hermes_inference_model=sa_block.get("hermes_inference_model", "operator-approved-model"),
        hermes_proxy_api_key=sa_block.get("hermes_proxy_api_key", "local"),
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
    if os.environ.get("HERMES_PROXY_URL"):
        cfg.hermes_proxy_url = os.environ["HERMES_PROXY_URL"]
    if os.environ.get("HERMES_INFERENCE_MODEL"):
        cfg.hermes_inference_model = os.environ["HERMES_INFERENCE_MODEL"]
    if os.environ.get("HERMES_PROXY_API_KEY"):
        cfg.hermes_proxy_api_key = os.environ["HERMES_PROXY_API_KEY"]

    return cfg
