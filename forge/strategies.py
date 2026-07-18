"""
forge/strategies.py — Strategy registry

A Strategy is a named parameter set. Forge simulates each one in parallel
against live data. Nothing is promoted automatically — results are logged
and humans pull/compare via the API or leaderboard.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Strategy:
    name: str
    symbol: str                  # "BTC/USDT" | "ETH/USDT" | "SOL/USDT" | "ALL"
    direction: str               # "long" | "short" | "both"

    # Signal confidence gates
    entry_threshold: float = 0.55
    exit_threshold: float  = 0.40

    # Risk parameters
    tp_atr_mult: float = 1.5
    sl_atr_mult: float = 1.0
    max_candles_held: int = 4    # force-close after N candles if no TP/SL hit

    # Metadata
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    notes: str = ""
    active: bool = True

    def to_dict(self) -> dict:
        return asdict(self)


# ── Default strategy grid ─────────────────────────────────────────────────────
# These are the baseline + a sweep of key parameters.
# Forge runs all of them simultaneously and logs results for comparison.

DEFAULT_STRATEGIES: list[Strategy] = [

    # ── BTC long ──────────────────────────────────────────────────────────────
    Strategy("btc_long_baseline",    "BTC/USDT", "long",  entry_threshold=0.55, tp_atr_mult=1.5, sl_atr_mult=1.0),
    Strategy("btc_long_tight_sl",    "BTC/USDT", "long",  entry_threshold=0.55, tp_atr_mult=1.5, sl_atr_mult=0.75),
    Strategy("btc_long_loose_tp",    "BTC/USDT", "long",  entry_threshold=0.55, tp_atr_mult=2.0, sl_atr_mult=1.0),
    Strategy("btc_long_hi_conf",     "BTC/USDT", "long",  entry_threshold=0.65, tp_atr_mult=1.5, sl_atr_mult=1.0),
    Strategy("btc_long_scalp",       "BTC/USDT", "long",  entry_threshold=0.55, tp_atr_mult=0.8, sl_atr_mult=0.5, max_candles_held=2),

    # ── BTC short ─────────────────────────────────────────────────────────────
    Strategy("btc_short_baseline",   "BTC/USDT", "short", entry_threshold=0.55, tp_atr_mult=1.5, sl_atr_mult=1.0),
    Strategy("btc_short_tight_sl",   "BTC/USDT", "short", entry_threshold=0.55, tp_atr_mult=1.5, sl_atr_mult=0.75),
    Strategy("btc_short_hi_conf",    "BTC/USDT", "short", entry_threshold=0.65, tp_atr_mult=1.5, sl_atr_mult=1.0),

    # ── ETH long ──────────────────────────────────────────────────────────────
    Strategy("eth_long_baseline",    "ETH/USDT", "long",  entry_threshold=0.55, tp_atr_mult=1.5, sl_atr_mult=1.0),
    Strategy("eth_long_hi_conf",     "ETH/USDT", "long",  entry_threshold=0.65, tp_atr_mult=1.5, sl_atr_mult=1.0),
    Strategy("eth_long_loose_tp",    "ETH/USDT", "long",  entry_threshold=0.55, tp_atr_mult=2.0, sl_atr_mult=1.0),

    # ── ETH short ─────────────────────────────────────────────────────────────
    Strategy("eth_short_baseline",   "ETH/USDT", "short", entry_threshold=0.55, tp_atr_mult=1.5, sl_atr_mult=1.0),
    Strategy("eth_short_hi_conf",    "ETH/USDT", "short", entry_threshold=0.65, tp_atr_mult=1.5, sl_atr_mult=1.0),

    # ── SOL short (long disabled in predictor — win rate too low) ─────────────
    Strategy("sol_short_baseline",   "SOL/USDT", "short", entry_threshold=0.55, tp_atr_mult=1.5, sl_atr_mult=1.0),
    Strategy("sol_short_hi_conf",    "SOL/USDT", "short", entry_threshold=0.65, tp_atr_mult=1.5, sl_atr_mult=1.0),
    Strategy("sol_short_scalp",      "SOL/USDT", "short", entry_threshold=0.60, tp_atr_mult=0.8, sl_atr_mult=0.5, max_candles_held=2),
]
