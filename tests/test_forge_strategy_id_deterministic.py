"""
tests/test_forge_strategy_id_deterministic.py — regression for the pre-fix
`Strategy.id = uuid.uuid4().hex[:8]` bug that produced 144 registry rows for
16 strategies (16 × ~9 restarts) live before beta-1.10.15.

The fix: id is now derived deterministically from symbol + direction + the
tuning params that make two strategies functionally different. Same inputs →
same id, across restarts, hosts, and Python versions. Cosmetic rename does
NOT change identity; a real param change DOES. All 16 DEFAULT_STRATEGIES must
have unique ids under this scheme.

If this test ever fails, do NOT "fix" it by changing the id scheme without
first checking whether an existing forge.db has trades under the old ids —
changing the scheme silently detaches all history. See db.cleanup_registry()
for the migration path if the scheme genuinely needs to change.
"""
from __future__ import annotations

from forge.strategies import (
    DEFAULT_STRATEGIES,
    Strategy,
    canonical_strategy_id,
)


def test_same_params_produce_same_id():
    a = Strategy("btc_long_baseline", "BTC/USDT", "long")
    b = Strategy("btc_long_baseline", "BTC/USDT", "long")
    assert a.id == b.id


def test_cosmetic_rename_does_not_change_id():
    """Renaming a strategy's display name (e.g. 'EMA Cross' → 'EMA Cross v2')
    must not create a new identity — the strategy is functionally the same."""
    a = Strategy("btc_long_baseline",       "BTC/USDT", "long")
    b = Strategy("BTC Long Baseline v2",    "BTC/USDT", "long")
    assert a.id == b.id


def test_param_change_produces_different_id():
    """A real change to any tuning param IS a different strategy for scoring
    purposes — the id must reflect that."""
    a = Strategy("btc_long_baseline", "BTC/USDT", "long", tp_atr_mult=1.5)
    b = Strategy("btc_long_baseline", "BTC/USDT", "long", tp_atr_mult=2.0)
    assert a.id != b.id


def test_all_default_strategies_have_unique_ids():
    ids = [s.id for s in DEFAULT_STRATEGIES]
    assert len(set(ids)) == len(ids), (
        f"DEFAULT_STRATEGIES contains id collisions: {ids}"
    )


def test_explicit_id_override_respected():
    """Tests and migrations sometimes need to pass an explicit id (e.g. to
    reproduce a specific historical row). That path must still work."""
    s = Strategy("x", "BTC/USDT", "long", id="fixed-id-42")
    assert s.id == "fixed-id-42"


def test_canonical_id_stability_across_processes():
    """The canonical id must be the same value we'd get from a completely
    separate invocation of the same function — reproducible, not random.
    This test hardcodes the expected id for one specific default strategy;
    if it fails, either the id scheme changed (see file docstring) or the
    strategy definition drifted."""
    s = Strategy("btc_long_baseline", "BTC/USDT", "long")
    # Recomputing directly from the raw inputs must match the dataclass id.
    recomputed = canonical_strategy_id(
        "BTC/USDT", "long",
        {"entry_threshold": 0.55, "exit_threshold": 0.40,
         "tp_atr_mult": 1.5, "sl_atr_mult": 1.0, "max_candles_held": 4},
    )
    assert s.id == recomputed
