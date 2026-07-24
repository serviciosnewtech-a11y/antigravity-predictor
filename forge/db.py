"""
forge/db.py — SQLite storage for Forge

Tables:
  candles              — rolling buffer of OHLCV + model predictions per symbol
  trades               — completed simulated trades (one row per closed position)
  strategy_registry    — registered strategies (identity + params + active flag)
  strategy_scorecard   — current per-strategy verdict (one row per strategy,
                         overwritten each scoring run)
  evaluation_history   — append-only log of every scoring run (substrate for
                         future trend-based verdicts like "declined for N runs")

WAL mode + synchronous=NORMAL are enabled on init: the scorecard job is a
background reader that must not block candle inserts, and vice versa. Default
DELETE journal mode serializes readers against writers, which for a live
insert path + a scheduled aggregate query means a briefly-stalled tick loop.
NORMAL fsync is safe with WAL and is the standard high-throughput setting.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from pathlib import Path
from typing import Any

# Relative default so this works both in Docker (WORKDIR /app, so
# "forge_data" resolves to /app/forge_data — identical to the old
# hardcoded absolute path) and bare-metal (resolves relative to wherever
# `python -m forge.server` is actually run from, matching the
# FORGE_DATA_DIR convention used elsewhere in .env.example /
# docker-compose.yml). A hardcoded "/app/forge_data" broke outright
# outside a container — there's no /app on a bare-metal host.
DB_PATH = Path(os.getenv("FORGE_DATA_DIR", "forge_data")) / "forge.db"
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    c.row_factory = sqlite3.Row
    # `synchronous` is a per-connection PRAGMA (unlike journal_mode, which
    # persists in the file header). Must be set on every new connection or
    # SQLite falls back to the FULL default and blocks on every fsync.
    # NORMAL is safe with WAL and is the standard high-throughput setting.
    c.execute("PRAGMA synchronous=NORMAL")
    return c


def init_db():
    with _lock, _conn() as c:
        # WAL: readers (scorecard job) never block writers (candle inserts) and
        # vice versa. Persists in the file header — set once, applies forever.
        # synchronous=NORMAL is per-connection and set in _conn() above.
        c.execute("PRAGMA journal_mode=WAL")

        c.executescript("""
        CREATE TABLE IF NOT EXISTS candles (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            ts             TEXT NOT NULL,
            symbol         TEXT NOT NULL,
            open           REAL,
            high           REAL,
            low            REAL,
            close          REAL,
            volume         REAL,
            atr            REAL,
            long_prob      REAL,
            short_prob     REAL,
            trend          TEXT,
            -- Nullable now, populated once retrain_all.sh writes a
            -- model manifest and the predictor emits versions in WS ticks.
            -- Cheap to add now, painful migration later.
            model_version  TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_candles_sym_ts ON candles(symbol, ts DESC);

        CREATE TABLE IF NOT EXISTS trades (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id        TEXT NOT NULL,
            strategy_name      TEXT NOT NULL,
            symbol             TEXT NOT NULL,
            direction          TEXT NOT NULL,
            entry_ts           TEXT NOT NULL,
            exit_ts            TEXT,
            entry_price        REAL,
            exit_price         REAL,
            tp_price           REAL,
            sl_price           REAL,
            exit_reason        TEXT,    -- "tp" | "sl" | "timeout" | "open"
            pnl_pct            REAL,
            candles_held       INTEGER,
            entry_conf         REAL,
            -- Nullable metadata columns; same reason as candles.model_version.
            model_version      TEXT,
            strategy_version   INTEGER  -- STRATEGY_ID_SCHEMA_VERSION at open time
        );
        CREATE INDEX IF NOT EXISTS idx_trades_strat    ON trades(strategy_id, entry_ts DESC);
        CREATE INDEX IF NOT EXISTS idx_trades_exit_ts  ON trades(exit_ts);

        CREATE TABLE IF NOT EXISTS strategy_registry (
            id              TEXT PRIMARY KEY,
            name            TEXT NOT NULL,
            symbol          TEXT,
            direction       TEXT,
            params          TEXT,    -- JSON blob
            active          INTEGER DEFAULT 1,
            created_ts      TEXT
        );

        -- Current scorecard: one row per strategy_id, overwritten each run.
        -- Read by GET /recommendations. Kept separate from evaluation_history
        -- so the dashboard/API stays O(1) per strategy regardless of how much
        -- history has accumulated.
        CREATE TABLE IF NOT EXISTS strategy_scorecard (
            strategy_id           TEXT PRIMARY KEY,
            strategy_name         TEXT NOT NULL,
            symbol                TEXT,
            direction             TEXT,
            trade_count           INTEGER,
            win_rate_pct          REAL,
            expectancy_pct        REAL,   -- avg pnl_pct per trade
            profit_factor         REAL,
            avg_R                 REAL,   -- avg_win / |avg_loss|
            max_drawdown_pct      REAL,   -- worst peak-to-trough on cumulative pnl_pct
            max_consec_losses     INTEGER,
            avg_candles_held      REAL,
            total_pnl_pct         REAL,
            verdict               TEXT NOT NULL,
            verdict_reason        TEXT NOT NULL,
            computed_ts           TEXT NOT NULL
        );

        -- Append-only history of every scorecard run. Enables future
        -- trend-based verdicts ("declined for N consecutive evaluations",
        -- "recovering") without needing a schema change to add them.
        CREATE TABLE IF NOT EXISTS evaluation_history (
            id                    INTEGER PRIMARY KEY AUTOINCREMENT,
            strategy_id           TEXT NOT NULL,
            strategy_name         TEXT NOT NULL,
            symbol                TEXT,
            direction             TEXT,
            trade_count           INTEGER,
            win_rate_pct          REAL,
            expectancy_pct        REAL,
            profit_factor         REAL,
            avg_R                 REAL,
            max_drawdown_pct      REAL,
            max_consec_losses     INTEGER,
            avg_candles_held      REAL,
            total_pnl_pct         REAL,
            verdict               TEXT NOT NULL,
            computed_ts           TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_eval_hist_strat
            ON evaluation_history(strategy_id, computed_ts DESC);
        """)

        # Additive migrations for pre-existing databases that were created
        # before these columns existed. `ALTER TABLE ADD COLUMN` is idempotent
        # in effect: we probe first, add only if missing. Safe on empty DBs
        # too (the CREATE TABLE above already includes the column, and the
        # probe finds it and skips).
        _add_column_if_missing(c, "candles", "model_version",    "TEXT")
        _add_column_if_missing(c, "trades",  "model_version",    "TEXT")
        _add_column_if_missing(c, "trades",  "strategy_version", "INTEGER")


def _add_column_if_missing(c: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    existing = {r["name"] for r in c.execute(f"PRAGMA table_info({table})").fetchall()}
    if col not in existing:
        c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


# ── Registry migration ───────────────────────────────────────────────────────

def cleanup_registry(known: dict[str, dict]) -> dict:
    """Idempotent migration of `strategy_registry` to canonical ids.

    `known` maps canonical_id → strategy dict (from Strategy.to_dict()).
    Does three things in one transaction, all safe to re-run:
      1. Rewrites `trades.strategy_id` for any historical row whose id is
         no longer canonical, matching by strategy_name if the params-inside
         match today's schema (best-effort: a strategy whose params were
         genuinely changed cannot be safely remapped and stays orphan).
      2. Deletes rows from strategy_registry whose id is not in `known`.
      3. Scrubs any stale `id` embedded in a registry row's `params` JSON
         blob so hydration from the DB never resurrects the old id.

    Returns a report dict {"registry_before", "registry_after", "trades_
    remapped", "orphan_trades", "params_scrubbed"} — printed by the caller.
    No silent deletes: caller logs the report.
    """
    report: dict[str, Any] = {}
    with _lock, _conn() as c:
        registry_before = c.execute(
            "SELECT COUNT(*) AS n FROM strategy_registry"
        ).fetchone()["n"]
        report["registry_before"] = registry_before

        # 1. Remap trades. Build a name → canonical_id lookup from `known`
        #    (names are unique in DEFAULT_STRATEGIES today; if a future
        #    duplicate appears, first entry wins — deterministic, at least).
        name_to_id: dict[str, str] = {}
        for cid, s in known.items():
            name_to_id.setdefault(s["name"], cid)

        remapped = 0
        for name, canonical_id in name_to_id.items():
            r = c.execute(
                "UPDATE trades SET strategy_id = ? "
                "WHERE strategy_name = ? AND strategy_id != ?",
                (canonical_id, name, canonical_id),
            )
            remapped += r.rowcount or 0
        report["trades_remapped"] = remapped

        # Count orphans (trades whose strategy_id is neither canonical
        # nor remapped by name — probably a strategy that was deleted).
        canonical_ids = set(known.keys())
        placeholders = ",".join("?" * len(canonical_ids)) if canonical_ids else "''"
        orphans = c.execute(
            f"SELECT COUNT(*) AS n FROM trades WHERE strategy_id NOT IN ({placeholders})",
            tuple(canonical_ids),
        ).fetchone()["n"]
        report["orphan_trades"] = orphans

        # 2. Delete non-canonical registry rows.
        if canonical_ids:
            deleted = c.execute(
                f"DELETE FROM strategy_registry WHERE id NOT IN ({placeholders})",
                tuple(canonical_ids),
            ).rowcount
        else:
            deleted = 0
        report["registry_deleted"] = deleted or 0

        # 3. Scrub stale `id` inside the `params` JSON of surviving rows.
        scrubbed = 0
        rows = c.execute("SELECT id, params FROM strategy_registry").fetchall()
        for row in rows:
            if not row["params"]:
                continue
            try:
                p = json.loads(row["params"])
            except (ValueError, TypeError):
                continue
            if p.get("id") != row["id"]:
                p["id"] = row["id"]
                c.execute(
                    "UPDATE strategy_registry SET params = ? WHERE id = ?",
                    (json.dumps(p), row["id"]),
                )
                scrubbed += 1
        report["params_scrubbed"] = scrubbed

        report["registry_after"] = c.execute(
            "SELECT COUNT(*) AS n FROM strategy_registry"
        ).fetchone()["n"]

    return report


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
    with _lock, _conn() as c:
        rows = c.execute("SELECT * FROM strategy_registry WHERE active=1").fetchall()
    return [json.loads(r["params"]) for r in rows]
