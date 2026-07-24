#!/usr/bin/env python3
"""
tools/forge_scorecard.py — periodic scoring pass over Forge trade history.

Reads forge_data/forge.db, runs forge.scoring.score_all_strategies(), and:
  1. Overwrites strategy_scorecard (current per-strategy verdict)
  2. Appends one row per strategy to evaluation_history (audit trail /
     substrate for future trend-based verdicts)
  3. Writes a plain-language human-readable dump to $FORGE_SCORECARD_DUMP
     (default: <parent-of-app-dir>/predictor-forge-scorecard.txt), outside
     the app dir on the same principle as the signal_history.db backup at
     tools/backup_signal_log.py — a wiped app dir shouldn't take the
     scorecard's human-facing view with it, and the dump lives on disk so
     the operator can `cat` it without needing the API running.

NOTHING promotes automatically. The scorecard is evidence; applying a
verdict is an explicit human action via POST /recommendations/apply.

Usage:
    python3 tools/forge_scorecard.py [--dump PATH]

Env vars (used by forge_scorecard.service):
    FORGE_DATA_DIR             Where forge.db lives (same var forge/db.py
                                reads). Default: forge_data (relative).
    FORGE_SCORECARD_DUMP       Where to write the human-readable text dump.
                                Default: derived from FORGE_DATA_DIR's
                                grandparent, e.g. /opt/predictor-forge-
                                scorecard.txt for /opt/predictor/forge_data.
    FORGE_SCORECARD_MIN_TRADES Minimum closed trades before rendering a
                                verdict other than 'not_enough_data'.
                                Default 50 (calibrated for 15m scalping —
                                see forge/scoring.py preamble).
    FORGE_SCORECARD_HEALTHY_PF          Default 1.3
    FORGE_SCORECARD_HEALTHY_MAX_DD      Default 15  (percent)
    FORGE_SCORECARD_HEALTHY_MAX_CONSEC_LOSS  Default 10
    FORGE_SCORECARD_LOSING_PF           Default 1.0
    FORGE_SCORECARD_LOSING_TOTAL_PNL    Default -3  (percent)
    FORGE_SCORECARD_LOSING_MAX_DD       Default 25  (percent)
"""
from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

# Ensure repo root is importable when the systemd unit runs this file directly
# (WorkingDirectory should already handle this, but being explicit costs
# nothing and makes the script runnable from any cwd for manual debugging).
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from forge import db, scoring
from forge.strategies import DEFAULT_STRATEGIES


def _default_dump_path() -> Path:
    env = os.environ.get("FORGE_SCORECARD_DUMP")
    if env:
        return Path(env)
    # forge_data is normally <app_dir>/forge_data, so the dump lands in a
    # sibling directory to the app dir (outside it entirely, same principle
    # as tools/backup_signal_log.py). e.g. /opt/predictor/forge_data →
    # /opt/predictor-forge-scorecard/scorecard.txt. Directory rather than
    # file directly under /opt so systemd ReadWritePaths can name a
    # specific dir instead of granting /opt-wide write access.
    data_dir = Path(os.environ.get("FORGE_DATA_DIR", "forge_data")).resolve()
    app_dir = data_dir.parent
    return app_dir.parent / f"{app_dir.name}-forge-scorecard" / "scorecard.txt"


def _render_text(rows: list[dict]) -> str:
    """Human-readable plain-text dump — no crypto jargon in the verdict line."""
    header = (
        f"Forge Strategy Scorecard\n"
        f"Generated: {time.strftime('%Y-%m-%d %H:%M:%S UTC', time.gmtime())}\n"
        f"Strategies scored: {len(rows)}\n"
        f"Verdict thresholds: min trades={scoring.MIN_TRADES}, "
        f"healthy PF≥{scoring.HEALTHY_PF}, losing PF<{scoring.LOSING_PF}\n"
        f"{'=' * 72}\n\n"
    )

    # Group by verdict so the operator sees the "losing" strategies first
    order = [
        (scoring.VERDICT_LOSING,          "Losing money — consider disabling"),
        (scoring.VERDICT_HEALTHY,         "Healthy — keep running"),
        (scoring.VERDICT_INCONCLUSIVE,    "Inconclusive — needs more time"),
        (scoring.VERDICT_NOT_ENOUGH_DATA, "Not enough data yet"),
    ]
    parts = [header]
    for verdict, title in order:
        matching = [r for r in rows if r["verdict"] == verdict]
        if not matching:
            continue
        parts.append(f"── {title} ({len(matching)}) {'─' * (60 - len(title))}\n")
        for r in matching:
            parts.append(
                f"  {r['strategy_name']}  ({r['symbol']} {r['direction']})\n"
                f"    {r['verdict_reason']}\n"
                f"    metrics: n={r['trade_count']} "
                f"WR={r['win_rate_pct']:.1f}% "
                f"PF={r['profit_factor']:.2f} "
                f"exp={r['expectancy_pct']:+.3f}% "
                f"maxDD={r['max_drawdown_pct']:.1f}% "
                f"maxConsecLoss={r['max_consec_losses']} "
                f"totalPnL={r['total_pnl_pct']:+.2f}%\n\n"
            )
    return "".join(parts)


def run_once(dump_path: Path) -> int:
    # init_db is safe to call on an existing DB — creates only what's missing.
    # This also ensures the DB file exists on a fresh install where forge/
    # server.py has never been started yet (the timer might fire before
    # forge.service ever has, and we still shouldn't blow up).
    db.init_db()

    known = [s.to_dict() for s in DEFAULT_STRATEGIES]
    rows = scoring.score_all_strategies(known=known)

    dump_path.parent.mkdir(parents=True, exist_ok=True)
    dump_path.write_text(_render_text(rows))
    print(f"[forge_scorecard] Scored {len(rows)} strategies, dump -> {dump_path}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dump", type=Path, default=None,
                         help="Where to write the human-readable text dump")
    args = parser.parse_args()
    dump = args.dump or _default_dump_path()
    return run_once(dump)


if __name__ == "__main__":
    sys.exit(main())
