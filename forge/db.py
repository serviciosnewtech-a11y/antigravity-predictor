"""
forge/db.py — SQLite storage for Forge

Two tables:
  candles  — rolling buffer of OHLCV + model predictions per symbol
  trades   — completed simulated trades (one row per closed position)
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any

DB_PATH = Path("/app/forge_data/forge.db")
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c


def init_db():
    with _lock, _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS candles (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            ts           TEXT NOT NULL,
            symbol       TEXT NOT NULL,
            open         REAL,
            high         REAL,
            low          REAL,
            close        REAL,
            volume       REAL,
            atr          REAL,
            long_prob    REAL,
            short_prob   REAL,
            trend        TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_candles_sym_ts ON candles(symbol, ts DESC);

        CREATE TABLE IF NOT EXISTS trades (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id     TEXT NOT NULL,
            strategy_name   TEXT NOT NULL,
            symbol          TEXT NOT NULL,
            direction       TEXT NOT NULL,
            entry_ts        TEXT NOT NULL,
            exit_ts         TEXT,
            entry_price     REAL,
            exit_price      REAL,
            tp_price        REAL,
            sl_price        REAL,
            exit_reason     TEXT,    -- "tp" | "sl" | "timeout" | "open"
            pnl_pct         REAL,
            candles_held    INTEGER,
            entry_conf      REAL
        );
        CREATE INDEX IF NOT EXISTS idx_trades_strat ON trades(strategy_id, entry_ts DESC);

        CREATE TABLE IF NOT EXISTS strategy_registry (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            symbol          TEXT,
            direction       TEXT,
            params          TEXT,    -- JSON blob
            active          INTEGER DEFAULT 1,
            created_ts      TEXT
        );
        """)


# ── Candles ───────────────────────────────────────────────────────────────────

def insert_candle(row: dict):
    with _lock, _conn() as c:
        c.execute("""
            INSERT INTO candles (ts,symbol,open,high,low,close,volume,atr,long_prob,short_prob,trend)
            VALUES (:ts,:symbol,:open,:high,:low,:close,:volume,:atr,:long_prob,:short_prob,:trend)
        """, row)
    # Prune old candles (keep last 5000 per symbol)
    with _lock, _conn() as c:
        c.execute("""
            DELETE FROM candles WHERE id IN (
                SELECT id FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT -1 OFFSET 5000
            )
        """, (row["symbol"],))


def get_candles(symbol: str, limit: int = 100) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM candles WHERE symbol=? ORDER BY ts DESC LIMIT ?",
            (symbol, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Trades ────────────────────────────────────────────────────────────────────

def open_trade(row: dict) -> int:
    with _lock, _conn() as c:
        cur = c.execute("""
            INSERT INTO trades
              (strategy_id,strategy_name,symbol,direction,entry_ts,entry_price,tp_price,sl_price,entry_conf,exit_reason)
            VALUES
              (:strategy_id,:strategy_name,:symbol,:direction,:entry_ts,:entry_price,:tp_price,:sl_price,:entry_conf,'open')
        """, row)
        return cur.lastrowid


def close_trade(trade_id: int, row: dict):
    with _lock, _conn() as c:
        c.execute("""
            UPDATE trades SET
                exit_ts=:exit_ts, exit_price=:exit_price, exit_reason=:exit_reason,
                pnl_pct=:pnl_pct, candles_held=:candles_held
            WHERE id=:id
        """, {**row, "id": trade_id})


def get_open_trades(strategy_id: str) -> list[dict]:
    with _lock, _conn() as c:
        rows = c.execute(
            "SELECT * FROM trades WHERE strategy_id=? AND exit_reason='open'",
            (strategy_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_trades(strategy_id: str | None = None, symbol: str | None = None,
               limit: int = 200) -> list[dict]:
    q = "SELECT * FROM trades WHERE exit_reason != 'open'"
    params: list[Any] = []
    if strategy_id:
        q += " AND strategy_id=?"; params.append(strategy_id)
    if symbol:
        q += " AND symbol=?"; params.append(symbol)
    q += " ORDER BY entry_ts DESC LIMIT ?"
    params.append(limit)
    with _lock, _conn() as c:
        rows = c.execute(q, params).fetchall()
    return [dict(r) for r in rows]


# ── Strategy registry ─────────────────────────────────────────────────────────

def upsert_strategy(s_dict: dict):
    import json
    with _lock, _conn() as c:
        c.execute("""
            INSERT OR REPLACE INTO strategy_registry (id,name,symbol,direction,params,active,created_ts)
            VALUES (:id,:name,:symbol,:direction,:params,:active,datetime('now'))
        """, {
            "id":        s_dict["id"],
            "name":      s_dict["name"],
            "symbol":    s_dict["symbol"],
            "direction": s_dict["direction"],
            "params":    json.dumps(s_dict),
            "active":    1 if s_dict["active"] else 0,
        })


def list_strategies() -> list[dict]:
    import json
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM strategy_registry WHERE active=1").fetchall()
    return [json.loads(r["params"]) for r in rows]
