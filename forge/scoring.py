"""
forge/scoring.py — evaluation loop for Forge strategies

One file, three sections:
  1. metrics       — pure functions that turn a list of closed trades into
                     a metrics dict (win rate, expectancy, profit factor,
                     avg R, max drawdown, max consecutive losses, etc.)
  2. evaluators    — apply thresholds to a metrics dict and return one of
                     four plain-language verdicts
  3. recommendations — orchestrator: read trades from db, compute metrics,
                     evaluate, persist to strategy_scorecard + evaluation_history

Kept as one file deliberately: for one evaluator against a fixed metric set,
splitting into three modules is layering for its own sake. Split into
`metrics.py` / `evaluators.py` / `recommendations.py` the moment a second
evaluator (Bayesian scoring, drift detection, ML ranking, etc.) shows up —
the section boundaries below map 1:1 to that future split.

All thresholds env-tunable — see the constants block near the top of §2.
Defaults are calibrated for 15-minute scalping on Bybit BTC/ETH/SOL (higher
noise ratio than swing timeframes → higher min-sample gate, tighter DD
tolerance, PF ≥ 1.3 for the "healthy" line).

NOTHING here changes strategy state. This is pure evidence — the operator
still applies verdicts by hand via POST /recommendations/apply.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from statistics import mean
from typing import Any, Iterable

from forge import db


# ═══════════════════════════════════════════════════════════════════════════════
# §1  METRICS — pure functions over a list of closed trade dicts
# ═══════════════════════════════════════════════════════════════════════════════

def compute_metrics(trades: list[dict]) -> dict[str, Any]:
    """Compute the full metric set for one strategy's closed trades.

    Trades come from db.get_trades(strategy_id=...) — dicts with pnl_pct,
    exit_reason, candles_held, entry_ts, exit_ts. Only rows with
    exit_reason in ('tp','sl','timeout') are counted; 'open' rows are
    skipped (db.get_trades already filters these out, but belt-and-braces).

    Returns a dict with numeric fields ready to be written to
    strategy_scorecard / evaluation_history. Empty trade list returns a
    zeroed dict — the evaluator's not_enough_data branch handles it.
    """
    closed = [
        t for t in trades
        if t.get("exit_reason") in ("tp", "sl", "timeout")
        and t.get("pnl_pct") is not None
    ]
    n = len(closed)
    if n == 0:
        return _zero_metrics()

    pnls = [float(t["pnl_pct"]) for t in closed]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]

    avg_win = mean(wins) if wins else 0.0
    avg_loss = mean(losses) if losses else 0.0

    # Profit factor: sum of gross wins / sum of gross losses. Standard
    # definition. Infinite if there are no losses (rare, but real for tiny
    # samples) — clamp to a large finite number so downstream comparisons /
    # JSON serialization don't need special-case handling.
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    if gross_loss == 0:
        pf = 999.0 if gross_win > 0 else 0.0
    else:
        pf = gross_win / gross_loss

    # Avg R (reward-to-risk realized): avg win size / avg loss size.
    # Complementary to win_rate — a low-WR trend follower can have great avg_R.
    if avg_loss != 0:
        avg_R = abs(avg_win / avg_loss)
    else:
        avg_R = 0.0

    # Max drawdown on the cumulative equity curve (in pnl_pct units).
    # Peak-to-trough distance. Trades sorted by exit_ts ascending; ties
    # broken by entry_ts. This is the actual live sequence of realized P&L.
    ordered = sorted(
        closed,
        key=lambda t: (t.get("exit_ts") or "", t.get("entry_ts") or ""),
    )
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    consec_loss = 0
    max_consec_loss = 0
    for t in ordered:
        p = float(t["pnl_pct"])
        running += p
        peak = max(peak, running)
        dd = peak - running
        max_dd = max(max_dd, dd)

        if p <= 0:
            consec_loss += 1
            max_consec_loss = max(max_consec_loss, consec_loss)
        else:
            consec_loss = 0

    held = [t["candles_held"] for t in closed if t.get("candles_held")]

    return {
        "trade_count":       n,
        "win_rate_pct":      round(len(wins) / n * 100, 2),
        "expectancy_pct":    round(mean(pnls), 4),
        "profit_factor":     round(pf, 3),
        "avg_R":             round(avg_R, 3),
        "max_drawdown_pct":  round(max_dd, 4),
        "max_consec_losses": max_consec_loss,
        "avg_candles_held":  round(mean(held), 2) if held else 0.0,
        "total_pnl_pct":     round(sum(pnls), 4),
    }


def _zero_metrics() -> dict[str, Any]:
    return {
        "trade_count":       0,
        "win_rate_pct":      0.0,
        "expectancy_pct":    0.0,
        "profit_factor":     0.0,
        "avg_R":             0.0,
        "max_drawdown_pct":  0.0,
        "max_consec_losses": 0,
        "avg_candles_held":  0.0,
        "total_pnl_pct":     0.0,
    }


# ═══════════════════════════════════════════════════════════════════════════════
# §2  EVALUATORS — apply thresholds, return a verdict
# ═══════════════════════════════════════════════════════════════════════════════

# Thresholds calibrated for 15-minute scalping. All env-overridable so Luis can
# tune without redeploying. Names mirror the env vars exactly.

MIN_TRADES              = int(os.getenv("FORGE_SCORECARD_MIN_TRADES", "50"))
HEALTHY_PF              = float(os.getenv("FORGE_SCORECARD_HEALTHY_PF", "1.3"))
HEALTHY_MAX_DD          = float(os.getenv("FORGE_SCORECARD_HEALTHY_MAX_DD", "15"))
HEALTHY_MAX_CONSEC_LOSS = int(os.getenv("FORGE_SCORECARD_HEALTHY_MAX_CONSEC_LOSS", "10"))
LOSING_PF               = float(os.getenv("FORGE_SCORECARD_LOSING_PF", "1.0"))
LOSING_TOTAL_PNL        = float(os.getenv("FORGE_SCORECARD_LOSING_TOTAL_PNL", "-3"))
LOSING_MAX_DD           = float(os.getenv("FORGE_SCORECARD_LOSING_MAX_DD", "25"))


# Four verdict labels. Machine-readable string on the left, human-readable
# label + explanation template built by _reason() below. Kept intentionally
# small — trend-based verdicts ("recovering", "degrading", "unstable")
# require evaluation_history depth to compute against, which won't exist
# until v2 has been running a while.
VERDICT_NOT_ENOUGH_DATA = "not_enough_data"
VERDICT_HEALTHY         = "healthy"
VERDICT_LOSING          = "losing_money_consider_disabling"
VERDICT_INCONCLUSIVE    = "inconclusive"


def evaluate(metrics: dict[str, Any]) -> tuple[str, str]:
    """Return (verdict, human_readable_reason).

    Order of checks matters:
      1. Sample gate first — nothing else is meaningful below it.
      2. Losing gate second — a losing strategy that also happens to have
         a high win rate on paper (tiny wins, big losses) should be flagged
         losing, not healthy. This is why the losing check runs before
         healthy.
      3. Healthy check requires all-of; anything else is inconclusive.
    """
    n = metrics.get("trade_count", 0)
    if n < MIN_TRADES:
        return VERDICT_NOT_ENOUGH_DATA, (
            f"Only {n} closed trades so far. Need at least {MIN_TRADES} "
            f"before a verdict means anything."
        )

    pf         = metrics.get("profit_factor", 0.0)
    total_pnl  = metrics.get("total_pnl_pct", 0.0)
    max_dd     = metrics.get("max_drawdown_pct", 0.0)
    max_streak = metrics.get("max_consec_losses", 0)
    expectancy = metrics.get("expectancy_pct", 0.0)
    win_rate   = metrics.get("win_rate_pct", 0.0)

    # Losing (any-of)
    if pf < LOSING_PF or total_pnl < LOSING_TOTAL_PNL or max_dd > LOSING_MAX_DD:
        pieces = []
        if pf < LOSING_PF:
            pieces.append(f"loses more than it wins (profit factor {pf:.2f} < {LOSING_PF})")
        if total_pnl < LOSING_TOTAL_PNL:
            pieces.append(f"cumulative P&L is {total_pnl:+.2f}% (worse than {LOSING_TOTAL_PNL:+.0f}%)")
        if max_dd > LOSING_MAX_DD:
            pieces.append(f"worst drawdown was {max_dd:.1f}% (over {LOSING_MAX_DD:.0f}%)")
        reason = (
            f"This strategy has {n} closed trades and it's bleeding — "
            + "; ".join(pieces) + ". Consider disabling."
        )
        return VERDICT_LOSING, reason

    # Healthy (all-of)
    if (pf >= HEALTHY_PF
            and expectancy > 0
            and max_dd < HEALTHY_MAX_DD
            and max_streak < HEALTHY_MAX_CONSEC_LOSS):
        reason = (
            f"Wins {win_rate:.0f}% of {n} trades, makes {expectancy:+.2f}% per trade "
            f"on average (profit factor {pf:.2f}), worst drawdown {max_dd:.1f}%. "
            f"Keep it running."
        )
        return VERDICT_HEALTHY, reason

    # Inconclusive
    reason = (
        f"Mixed signal across {n} trades: win rate {win_rate:.0f}%, "
        f"avg trade {expectancy:+.2f}%, profit factor {pf:.2f}, "
        f"drawdown {max_dd:.1f}%. Not clearly good or bad — give it more time."
    )
    return VERDICT_INCONCLUSIVE, reason


# ═══════════════════════════════════════════════════════════════════════════════
# §3  RECOMMENDATIONS — orchestrator, persistence
# ═══════════════════════════════════════════════════════════════════════════════

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def score_all_strategies(known: Iterable[dict] | None = None) -> list[dict]:
    """Compute + persist scorecards for every strategy in strategy_registry.

    `known` is an optional iterable of strategy dicts (from Strategy.to_dict())
    used to include strategies that exist in code but have no trades yet — so
    they show up in the scorecard as `not_enough_data` rather than being
    invisible. When None, only strategies present in strategy_registry are
    scored.

    Persistence:
      - strategy_scorecard: one row per strategy_id, overwritten each run
      - evaluation_history: one row per strategy_id per run (append-only)

    Returns the list of scorecard rows just written.
    """
    computed_ts = _now_iso()

    # Union of registry rows + known strategies (dedup on id). Registry rows
    # supply symbol/direction; known strategies fill in any strategy that
    # exists in code but hasn't been logged into the registry yet.
    registry = {r["id"]: r for r in db.list_strategies()}
    if known:
        for s in known:
            registry.setdefault(s["id"], s)

    rows_written: list[dict] = []
    for sid, sinfo in registry.items():
        trades = db.get_trades(strategy_id=sid, limit=1_000_000)
        metrics = compute_metrics(trades)
        verdict, reason = evaluate(metrics)

        row = {
            "strategy_id":       sid,
            "strategy_name":     sinfo.get("name", "?"),
            "symbol":            sinfo.get("symbol"),
            "direction":         sinfo.get("direction"),
            "verdict":           verdict,
            "verdict_reason":    reason,
            "computed_ts":       computed_ts,
            **metrics,
        }
        _upsert_scorecard(row)
        _append_history(row)
        rows_written.append(row)

    return rows_written


def _upsert_scorecard(row: dict) -> None:
    with db._lock, db._conn() as c:
        c.execute(
            """
            INSERT OR REPLACE INTO strategy_scorecard (
                strategy_id, strategy_name, symbol, direction,
                trade_count, win_rate_pct, expectancy_pct, profit_factor,
                avg_R, max_drawdown_pct, max_consec_losses, avg_candles_held,
                total_pnl_pct, verdict, verdict_reason, computed_ts
            ) VALUES (
                :strategy_id, :strategy_name, :symbol, :direction,
                :trade_count, :win_rate_pct, :expectancy_pct, :profit_factor,
                :avg_R, :max_drawdown_pct, :max_consec_losses, :avg_candles_held,
                :total_pnl_pct, :verdict, :verdict_reason, :computed_ts
            )
            """,
            row,
        )


def _append_history(row: dict) -> None:
    with db._lock, db._conn() as c:
        c.execute(
            """
            INSERT INTO evaluation_history (
                strategy_id, strategy_name, symbol, direction,
                trade_count, win_rate_pct, expectancy_pct, profit_factor,
                avg_R, max_drawdown_pct, max_consec_losses, avg_candles_held,
                total_pnl_pct, verdict, computed_ts
            ) VALUES (
                :strategy_id, :strategy_name, :symbol, :direction,
                :trade_count, :win_rate_pct, :expectancy_pct, :profit_factor,
                :avg_R, :max_drawdown_pct, :max_consec_losses, :avg_candles_held,
                :total_pnl_pct, :verdict, :computed_ts
            )
            """,
            row,
        )


def get_scorecard() -> list[dict]:
    """Read the current scorecard from strategy_scorecard, sorted by name."""
    with db._lock, db._conn() as c:
        rows = c.execute(
            "SELECT * FROM strategy_scorecard ORDER BY strategy_name"
        ).fetchall()
    return [dict(r) for r in rows]


def get_evaluation_history(strategy_id: str, limit: int = 200) -> list[dict]:
    with db._lock, db._conn() as c:
        rows = c.execute(
            "SELECT * FROM evaluation_history WHERE strategy_id = ? "
            "ORDER BY computed_ts DESC LIMIT ?",
            (strategy_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def apply_recommendation(strategy_id: str) -> dict:
    """Human-approved application of a scorecard verdict.

    Only meaningful for VERDICT_LOSING today: sets active=0 in
    strategy_registry. Other verdicts are no-ops that return the current
    state. Deliberately explicit / manual — see §3 preamble.

    Returns the updated registry row.
    """
    with db._lock, db._conn() as c:
        sc = c.execute(
            "SELECT verdict FROM strategy_scorecard WHERE strategy_id = ?",
            (strategy_id,),
        ).fetchone()
        if sc is None:
            raise ValueError(f"No scorecard for strategy_id={strategy_id}")

        reg = c.execute(
            "SELECT * FROM strategy_registry WHERE id = ?", (strategy_id,)
        ).fetchone()
        if reg is None:
            raise ValueError(f"No registry entry for strategy_id={strategy_id}")

        verdict = sc["verdict"]
        if verdict == VERDICT_LOSING:
            c.execute(
                "UPDATE strategy_registry SET active = 0 WHERE id = ?",
                (strategy_id,),
            )
            action = "deactivated"
        else:
            action = "no_op"

        after = c.execute(
            "SELECT * FROM strategy_registry WHERE id = ?", (strategy_id,)
        ).fetchone()

    return {"action": action, "verdict": verdict, "strategy": dict(after)}
