"""
tests/test_forge_scorecard_end_to_end.py — full pipeline regression:
seed synthetic trades in an isolated DB, run tools/forge_scorecard.py
end-to-end, assert strategy_scorecard + evaluation_history are populated
correctly and the human-readable dump file exists.

This is the "does the runner script actually work as a whole" test — the
verdict-branch and metric-arithmetic details are covered by
test_forge_scorecard_verdicts.py; this one is about wiring.
"""
from __future__ import annotations

import importlib
import os
import random
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def forge_env(monkeypatch):
    tmp = Path(tempfile.mkdtemp())
    monkeypatch.setenv("FORGE_DATA_DIR", str(tmp / "forge_data"))
    monkeypatch.setenv("FORGE_SCORECARD_DUMP", str(tmp / "scorecard.txt"))
    # Force-reload the forge modules so DB_PATH re-resolves.
    for mod in ("forge.scoring", "forge.db", "forge"):
        if mod in sys.modules:
            del sys.modules[mod]
    return tmp


def _seed(db_path: Path, healthy_id, healthy_name, losing_id, losing_name,
          few_id, few_name):
    conn = sqlite3.connect(str(db_path))
    random.seed(42)

    # 60 shuffled trades → healthy
    pnls = [0.5] * 40 + [-0.4] * 20
    random.shuffle(pnls)
    for i, p in enumerate(pnls):
        conn.execute(
            "INSERT INTO trades "
            "(strategy_id,strategy_name,symbol,direction,entry_ts,exit_ts,"
            "pnl_pct,exit_reason,candles_held) VALUES (?,?,?,?,?,?,?,?,?)",
            (healthy_id, healthy_name, "BTC/USDT", "long",
             f"2026-07-01T00:00:{i:02d}", f"2026-07-01T00:15:{i:02d}",
             p, "tp" if p > 0 else "sl", 3),
        )

    # 60 shuffled trades → losing
    pnls = [0.5] * 20 + [-0.5] * 40
    random.shuffle(pnls)
    for i, p in enumerate(pnls):
        conn.execute(
            "INSERT INTO trades "
            "(strategy_id,strategy_name,symbol,direction,entry_ts,exit_ts,"
            "pnl_pct,exit_reason,candles_held) VALUES (?,?,?,?,?,?,?,?,?)",
            (losing_id, losing_name, "SOL/USDT", "short",
             f"2026-07-03T00:00:{i:02d}", f"2026-07-03T00:15:{i:02d}",
             p, "tp" if p > 0 else "sl", 3),
        )

    # 5 trades → not_enough_data
    for i, p in enumerate([0.2, -0.1, 0.3, -0.2, 0.1]):
        conn.execute(
            "INSERT INTO trades "
            "(strategy_id,strategy_name,symbol,direction,entry_ts,exit_ts,"
            "pnl_pct,exit_reason,candles_held) VALUES (?,?,?,?,?,?,?,?,?)",
            (few_id, few_name, "BTC/USDT", "long",
             f"2026-07-02T{i:02d}:00:00", f"2026-07-02T{i:02d}:15:00",
             p, "tp" if p > 0 else "sl", 2),
        )
    conn.commit()
    conn.close()


def test_runner_populates_scorecard_history_and_dump(forge_env, monkeypatch):
    from forge import db, strategies

    db.init_db()
    known = {s.id: s.to_dict() for s in strategies.DEFAULT_STRATEGIES}
    db.cleanup_registry(known)
    for s in strategies.DEFAULT_STRATEGIES:
        db.upsert_strategy(s.to_dict())

    healthy = next(s for s in strategies.DEFAULT_STRATEGIES if s.name == "btc_long_baseline")
    losing  = next(s for s in strategies.DEFAULT_STRATEGIES if s.name == "sol_short_hi_conf")
    few     = next(s for s in strategies.DEFAULT_STRATEGIES if s.name == "btc_long_scalp")

    _seed(db.DB_PATH, healthy.id, healthy.name, losing.id, losing.name, few.id, few.name)

    # Invoke the runner script the exact way the systemd unit would.
    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    result = subprocess.run(
        [sys.executable, str(REPO_ROOT / "tools" / "forge_scorecard.py")],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, (
        f"runner failed: stdout={result.stdout!r} stderr={result.stderr!r}"
    )

    # 1. strategy_scorecard: one row per strategy, verdicts as expected.
    conn = sqlite3.connect(str(db.DB_PATH))
    conn.row_factory = sqlite3.Row
    sc = {
        r["strategy_id"]: r["verdict"]
        for r in conn.execute("SELECT * FROM strategy_scorecard").fetchall()
    }
    assert sc[healthy.id] == "healthy"
    assert sc[losing.id]  == "losing_money_consider_disabling"
    assert sc[few.id]     == "not_enough_data"
    assert len(sc) == len(strategies.DEFAULT_STRATEGIES)

    # 2. evaluation_history: one row per strategy after one run.
    h = conn.execute("SELECT COUNT(*) FROM evaluation_history").fetchone()[0]
    assert h == len(strategies.DEFAULT_STRATEGIES)
    conn.close()

    # 3. Text dump exists, is non-empty, and mentions all three verdicts.
    dump = Path(os.environ["FORGE_SCORECARD_DUMP"])
    assert dump.exists()
    text = dump.read_text()
    assert "Losing money" in text
    assert "Healthy" in text
    assert "Not enough data" in text
    assert healthy.name in text
    assert losing.name  in text


def test_second_run_appends_history_and_overwrites_scorecard(forge_env, monkeypatch):
    """Runner is a timer — must be safe to invoke repeatedly. scorecard
    grows to N rows (overwrite semantics), history grows by N per run."""
    from forge import db, strategies

    db.init_db()
    for s in strategies.DEFAULT_STRATEGIES:
        db.upsert_strategy(s.to_dict())

    env = os.environ.copy()
    env["PYTHONPATH"] = str(REPO_ROOT)
    cmd = [sys.executable, str(REPO_ROOT / "tools" / "forge_scorecard.py")]

    subprocess.run(cmd, env=env, check=True, capture_output=True)
    subprocess.run(cmd, env=env, check=True, capture_output=True)
    subprocess.run(cmd, env=env, check=True, capture_output=True)

    conn = sqlite3.connect(str(db.DB_PATH))
    n_sc = conn.execute("SELECT COUNT(*) FROM strategy_scorecard").fetchone()[0]
    n_hist = conn.execute("SELECT COUNT(*) FROM evaluation_history").fetchone()[0]
    conn.close()

    N = len(strategies.DEFAULT_STRATEGIES)
    assert n_sc == N, "scorecard should stay at N rows across runs (overwrite)"
    assert n_hist == 3 * N, "history should accumulate N rows per run"
