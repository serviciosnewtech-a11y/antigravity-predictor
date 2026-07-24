"""
forge/strategies.py — Strategy registry

A Strategy is a named parameter set. Forge simulates each one in parallel
against live data. Nothing is promoted automatically — results are logged
and humans pull/compare via the API or leaderboard.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional

# Fixed namespace UUID for deterministic Strategy.id derivation. Do NOT change
# once real trades exist — every strategy_id in `trades` derives from this
# namespace, and changing it silently re-generates every id (fragmenting
# history the same way random uuid4() used to). Generated once with
# uuid.uuid5(uuid.NAMESPACE_URL, "https://antigravity-predictor/forge/strategy").
FORGE_STRATEGY_NAMESPACE = uuid.UUID("e6a5f7a9-3c1e-5b8a-9d4f-7e2c1a3b5d6f")

# Bumped when the identity-fields set below changes (add/remove/rename a
# field that participates in the hash). Baked into the canonical string so
# same params under different schema versions get different ids — protects
# against silent identity collisions if the definition of "same strategy"
# ever changes. See _canonical_id_string() below.
STRATEGY_ID_SCHEMA_VERSION = 1


def _canonical_id_string(symbol: str, direction: str, params: dict) -> str:
    """Canonical string used to derive Strategy.id.

    Deliberately excludes `name` (cosmetic — "EMA Cross" → "EMA Cross v2"
    should not create a new identity) and metadata fields like `notes`,
    `active`, `id` itself. Includes exactly the tuning params that make two
    strategies functionally different: symbol, direction, and the risk /
    signal-gate numbers below.

    JSON with sort_keys=True gives a stable serialization regardless of
    dict insertion order.
    """
    return json.dumps({
        "v":         STRATEGY_ID_SCHEMA_VERSION,
        "symbol":    symbol,
        "direction": direction,
        "params":    params,
    }, sort_keys=True, separators=(",", ":"))


def canonical_strategy_id(symbol: str, direction: str, params: dict) -> str:
    """Derive a stable 12-char id from a strategy's identity-relevant fields.

    Same inputs → same id, across restarts, hosts, and Python versions.
    Different tuning params → different id (which is correct: it IS a
    different strategy for scoring purposes).
    """
    canonical = _canonical_id_string(symbol, direction, params)
    return uuid.uuid5(FORGE_STRATEGY_NAMESPACE, canonical).hex[:12]


# Fields that participate in identity. Order irrelevant (json sort_keys=True).
# If this list changes, bump STRATEGY_ID_SCHEMA_VERSION above.
_ID_PARAM_FIELDS = (
    "entry_threshold",
    "exit_threshold",
    "tp_atr_mult",
    "sl_atr_mult",
    "max_candles_held",
)


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

    # Metadata. `id` is derived from the identity fields in __post_init__
    # unless explicitly supplied (tests/migrations may pass one).
    id: str = ""
    notes: str = ""
    active: bool = True

    def __post_init__(self):
        if not self.id:
            self.id = canonical_strategy_id(
                self.symbol,
                self.direction,
                {f: getattr(self, f) for f in _ID_PARAM_FIELDS},
            )

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
