"""
signal_log.py — durable, append-only archive of every signal transition and
every completed simulated trade the predictor produces.

Why this exists: AssetEngine.trades_history (in predictor_server.py) was an
in-memory Python list only. Every process restart — a crash, a redeploy, a
`docker compose restart`, any of the several the predictor has been through
in one day of real deployment — silently wiped it back to zero. There was no
way to answer "did today's BUY signal on ETH actually hit TP?" a day later,
and no data to eventually retrain thresholds against. This module exists so
that stops being true: it's the raw material for actually improving the
system later, not just a live dashboard number that resets on restart.

SQLite, same proven pattern as forge/db.py. Lives under logs/ rather than
data/ or models/ deliberately — those two are mounted read-only in the
Docker deployment (see deploy/docker/docker-compose.yml), logs/ is the one
directory writable in both the Docker and bare-metal paths.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path
from typing import Any, Optional

_LOGS_DIR = Path(
    os.environ.get("LOGS_DIR")
    or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
)
DB_PATH = _LOGS_DIR / "signal_history.db"
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db() -> None:
    with _lock, _conn() as c:
        c.executescript(
            """
            CREATE TABLE IF NOT EXISTS trades (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol          TEXT NOT NULL,
                direction       TEXT NOT NULL,   -- "LONG" | "SHORT"
                entry_time      INTEGER NOT NULL,
                exit_time       INTEGER NOT NULL,
                entry_price     REAL,
                exit_price      REAL,
                pnl_usdt        REAL,
                pnl_pct         REAL,
                exit_reason     TEXT,            -- "Take Profit" | "Stop Loss" | "Exit Signal" | "Time Decay"
                recorded_at     TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_trades_symbol_entry ON trades(symbol, entry_time DESC);

            CREATE TABLE IF NOT EXISTS signal_events (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              INTEGER NOT NULL,
                symbol          TEXT NOT NULL,
                signal          TEXT NOT NULL,   -- "BUY" | "SELL" | "NEUTRAL" | "EXIT" | "UNAVAILABLE"
                long_prob       REAL,
                short_prob      REAL,
                price           REAL,
                atr             REAL,
                degraded        INTEGER,
                recorded_at     TEXT DEFAULT (datetime('now'))
            );
            CREATE INDEX IF NOT EXISTS idx_signal_events_symbol_ts ON signal_events(symbol, ts DESC);
            """
        )


def record_trade(row: dict[str, Any]) -> None:
    """row keys match AssetEngine.trades_history's own dict shape exactly —
    symbol, type, entry_time, exit_time, entry_price, exit_price, pnl,
    pnl_pct, reason — so callers can pass that dict straight through."""
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO trades
               (symbol, direction, entry_time, exit_time, entry_price, exit_price, pnl_usdt, pnl_pct, exit_reason)
               VALUES (:symbol, :type, :entry_time, :exit_time, :entry_price, :exit_price, :pnl, :pnl_pct, :reason)""",
            row,
        )


def record_signal_event(
    ts: int, symbol: str, signal: str, long_prob: float, short_prob: float,
    price: Optional[float], atr: Optional[float], degraded: bool,
) -> None:
    with _lock, _conn() as c:
        c.execute(
            """INSERT INTO signal_events (ts, symbol, signal, long_prob, short_prob, price, atr, degraded)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (ts, symbol, signal, long_prob, short_prob, price, atr, 1 if degraded else 0),
        )


def get_trades(symbol: Optional[str] = None, limit: int = 500) -> list[dict]:
    q = "SELECT * FROM trades"
    params: list[Any] = []
    if symbol:
        q += " WHERE symbol=?"
        params.append(symbol)
    q += " ORDER BY entry_time DESC LIMIT ?"
    params.append(limit)
    with _lock, _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_signal_events(symbol: Optional[str] = None, limit: int = 500) -> list[dict]:
    q = "SELECT * FROM signal_events"
    params: list[Any] = []
    if symbol:
        q += " WHERE symbol=?"
        params.append(symbol)
    q += " ORDER BY ts DESC LIMIT ?"
    params.append(limit)
    with _lock, _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


def get_stats(symbol: Optional[str] = None) -> dict:
    """Aggregate stats to reseed AssetEngine.total_pnl/win_trades/loss_trades
    on startup, so a restart doesn't visibly reset a symbol's whole trading
    history to zero — same numbers, just no longer memory-only."""
    q = (
        "SELECT COUNT(*) AS total_trades, "
        "SUM(CASE WHEN pnl_usdt >= 0 THEN 1 ELSE 0 END) AS win_trades, "
        "SUM(CASE WHEN pnl_usdt < 0 THEN 1 ELSE 0 END) AS loss_trades, "
        "COALESCE(SUM(pnl_usdt), 0.0) AS total_pnl "
        "FROM trades"
    )
    params: list[Any] = []
    if symbol:
        q += " WHERE symbol=?"
        params.append(symbol)
    with _lock, _conn() as c:
        row = c.execute(q, params).fetchone()
    d = dict(row) if row else {}
    return {
        "total_trades": d.get("total_trades") or 0,
        "win_trades": d.get("win_trades") or 0,
        "loss_trades": d.get("loss_trades") or 0,
        "total_pnl": d.get("total_pnl") or 0.0,
    }
