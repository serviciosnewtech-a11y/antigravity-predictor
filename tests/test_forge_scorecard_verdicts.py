"""
tests/test_forge_scorecard_verdicts.py — regression for the four scorecard
verdict branches (not_enough_data / healthy / losing / inconclusive) and
the metrics that feed them.

Thresholds are calibrated for 15-minute scalping (see forge/scoring.py
preamble). All are env-tunable — if a test fails after someone changes
FORGE_SCORECARD_*, check whether the intent was to move the threshold or
whether the test synthetic data needs to move with it.

The metric calculations (max drawdown, max consecutive losses, profit
factor, expectancy) also have hand-computed reference cases here so a
future refactor of scoring.py can't silently drift them without breaking
this test.
"""
from __future__ import annotations

from forge.scoring import (
    HEALTHY_MAX_CONSEC_LOSS,
    HEALTHY_MAX_DD,
    HEALTHY_PF,
    LOSING_MAX_DD,
    LOSING_PF,
    LOSING_TOTAL_PNL,
    MIN_TRADES,
    VERDICT_HEALTHY,
    VERDICT_INCONCLUSIVE,
    VERDICT_LOSING,
    VERDICT_NOT_ENOUGH_DATA,
    compute_metrics,
    evaluate,
)


def _mk(pnls, exit_reason_default="tp"):
    """Build synthetic trade dicts. pnl_pct > 0 → 'tp', <= 0 → 'sl'."""
    return [
        {
            "pnl_pct":      p,
            "exit_reason":  "tp" if p > 0 else "sl",
            "candles_held": 3,
            "entry_ts":     f"2026-07-01T{i:02d}:00:00",
            "exit_ts":      f"2026-07-01T{i:02d}:15:00",
        }
        for i, p in enumerate(pnls)
    ]


# ── §1 metrics: hand-computed reference ─────────────────────────────────────

def test_metrics_reference_5_trades():
    """Reference values computed by hand — do NOT change these unless the
    definition of a metric legitimately changes."""
    m = compute_metrics(_mk([2.0, -1.0, 3.0, -0.5, 1.0]))
    assert m["trade_count"]      == 5
    assert m["win_rate_pct"]     == 60.0
    assert m["expectancy_pct"]   == 0.9         # (2-1+3-0.5+1)/5
    assert m["profit_factor"]    == 4.0         # 6.0 / 1.5
    assert abs(m["avg_R"] - 8/3) < 0.01         # 2.0 / 0.75
    assert m["max_drawdown_pct"] == 1.0         # peak 2, trough 1
    assert m["max_consec_losses"] == 1
    assert m["total_pnl_pct"]    == 4.5


def test_metrics_empty_returns_zero():
    m = compute_metrics([])
    assert m["trade_count"] == 0
    assert m["profit_factor"] == 0.0


def test_metrics_max_drawdown_deep():
    """Multiple wins then a run of losses — max DD must be the total
    depth from the equity peak, not just the biggest single loss."""
    m = compute_metrics(_mk([1, 1, 1, -0.5, -0.5, -0.5, -0.5, 1]))
    # peak = 3, trough = 1, DD = 2
    assert m["max_drawdown_pct"] == 2.0
    assert m["max_consec_losses"] == 4


def test_metrics_profit_factor_no_losses():
    """Edge case: strategy has never lost. PF must be finite (JSON-
    serializable) not float('inf')."""
    m = compute_metrics(_mk([1, 2, 3]))
    assert m["profit_factor"] < 10000  # capped, not infinite
    assert m["profit_factor"] > 0


# ── §2 verdicts: all four branches ──────────────────────────────────────────

def test_verdict_not_enough_data():
    """Any trade count strictly below MIN_TRADES → not_enough_data,
    regardless of what the metrics look like."""
    for n in (0, 1, MIN_TRADES - 1):
        pnls = [0.5] * n
        v, reason = evaluate(compute_metrics(_mk(pnls)))
        assert v == VERDICT_NOT_ENOUGH_DATA, (
            f"n={n} produced {v} instead of not_enough_data: {reason}"
        )
        assert str(MIN_TRADES) in reason


def test_verdict_healthy():
    """Sample gate met, PF ≥ HEALTHY_PF, positive expectancy, low DD,
    low consecutive-loss streak → healthy."""
    # 60 trades, PF ~2.5, WR 66%, tiny DD when shuffled
    import random
    random.seed(42)
    pnls = [0.5] * 40 + [-0.4] * 20
    random.shuffle(pnls)
    v, reason = evaluate(compute_metrics(_mk(pnls)))
    assert v == VERDICT_HEALTHY, f"expected healthy, got {v}: {reason}"


def test_verdict_losing_by_profit_factor():
    """PF strictly below LOSING_PF → losing, regardless of other metrics."""
    import random
    random.seed(1)
    pnls = [0.5] * 20 + [-0.5] * 40  # PF = 10/20 = 0.5
    random.shuffle(pnls)
    v, reason = evaluate(compute_metrics(_mk(pnls)))
    assert v == VERDICT_LOSING, f"expected losing, got {v}: {reason}"
    assert "profit factor" in reason


def test_verdict_losing_by_total_pnl():
    """Total pnl worse than LOSING_TOTAL_PNL → losing (even if PF ≥ 1.0)."""
    # Craft: 30 wins of +0.1 = 3, 30 losses of -0.2 = -6. Total = -3.
    # PF = 3 / 6 = 0.5 — also triggers pf branch, so this test really
    # exercises "at least one losing condition triggers, message includes
    # the condition that fired". LOSING is any-of.
    pnls = [0.1] * 30 + [-0.2] * 30
    import random; random.seed(2); random.shuffle(pnls)
    v, reason = evaluate(compute_metrics(_mk(pnls)))
    assert v == VERDICT_LOSING


def test_verdict_inconclusive():
    """Sample gate met, neither losing-any-of nor healthy-all-of → inconclusive."""
    # Craft: 60 trades where PF ~1.14 (between LOSING_PF=1.0 and HEALTHY_PF=1.3),
    # DD < 15%, total pnl >= -3%, low consec-loss streak.
    import random
    random.seed(3)
    pnls = [0.4] * 30 + [-0.35] * 30  # PF ~1.14, total ~+1.5
    random.shuffle(pnls)
    v, reason = evaluate(compute_metrics(_mk(pnls)))
    assert v == VERDICT_INCONCLUSIVE, f"expected inconclusive, got {v}: {reason}"


def test_verdict_losing_takes_precedence_over_healthy_appearance():
    """A strategy with a high WR but tiny wins vs big losses (net negative)
    must be flagged losing, not healthy. This is the reason evaluate()
    checks the losing branch before the healthy branch."""
    # 40 wins of +0.05 = +2.0, 20 losses of -1.0 = -20. WR = 66.7%. Total = -18.
    # PF = 2 / 20 = 0.1. Triggers pf-based losing.
    import random
    pnls = [0.05] * 40 + [-1.0] * 20
    random.seed(4); random.shuffle(pnls)
    v, _ = evaluate(compute_metrics(_mk(pnls)))
    assert v == VERDICT_LOSING


def test_thresholds_readable_from_env(monkeypatch):
    """Sanity: the thresholds are actually read from env vars (regression
    against someone hardcoding them by mistake)."""
    # Reload the module with a custom threshold set.
    monkeypatch.setenv("FORGE_SCORECARD_MIN_TRADES", "5")
    import importlib
    import forge.scoring as sc
    importlib.reload(sc)
    try:
        assert sc.MIN_TRADES == 5
        v, _ = sc.evaluate(sc.compute_metrics(
            [{"pnl_pct": 1.0, "exit_reason": "tp", "candles_held": 1,
              "entry_ts": "2026-07-01T00:00:00", "exit_ts": "2026-07-01T00:15:00"}] * 5
        ))
        assert v != sc.VERDICT_NOT_ENOUGH_DATA
    finally:
        monkeypatch.delenv("FORGE_SCORECARD_MIN_TRADES", raising=False)
        importlib.reload(sc)  # restore defaults for other tests
