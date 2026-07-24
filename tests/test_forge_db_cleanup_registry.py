"""
tests/test_forge_db_cleanup_registry.py — regression for the fragmented
strategy_registry migration path. Pre-fix live state: 144 rows for what
should have been 16 strategies (16 × 9 restarts, each generating fresh
random ids). The migration must:

  1. remap trades whose strategy_id is not canonical but whose
     strategy_name matches a known canonical strategy (best-effort);
  2. delete non-canonical registry rows;
  3. scrub any stale `id` embedded in a registry row's `params` JSON blob;
  4. leave orphan trades in place (never silently delete) — those may be
     from strategies that were genuinely removed from code and shouldn't
     be re-attributed;
  5. be idempotent — running the migration twice must produce identical
     state to running it once.

If this test fails, do NOT skip it or loosen the assertions — this is
exactly the class of bug the migration was designed to prevent.
"""
from __future__ import annotations

import json
import sqlite3
import uuid as _uuid

# `forge_db` fixture is provided by tests/conftest.py.


def _seed_fragmented(db, strategies_list, restarts: int):
    """Simulate `restarts` startups each generating fresh random ids for
    every strategy — the pre-fix live state that produced 144 rows."""
    conn = sqlite3.connect(str(db.DB_PATH))
    conn.row_factory = sqlite3.Row
    for _ in range(restarts):
        for s in strategies_list:
            fake_id = _uuid.uuid4().hex[:8]
            p = s.to_dict()
            p["id"] = fake_id  # stale id inside params blob — must be scrubbed
            conn.execute(
                "INSERT INTO strategy_registry "
                "(id,name,symbol,direction,params,active,created_ts) "
                "VALUES (?,?,?,?,?,?,?)",
                (fake_id, s.name, s.symbol, s.direction,
                 json.dumps(p), 1, "2026-07-20"),
            )
            conn.execute(
                "INSERT INTO trades "
                "(strategy_id,strategy_name,symbol,direction,entry_ts,"
                "pnl_pct,exit_reason) VALUES (?,?,?,?,?,?,?)",
                (fake_id, s.name, s.symbol, s.direction,
                 "2026-07-20T10:00:00", 0.5, "tp"),
            )
    conn.commit()
    conn.close()


def _register_orphan(db):
    """A trade whose strategy is not in the current known set — this must
    survive the migration (orphan trades belong to strategies removed from
    code; silently deleting them would erase real history)."""
    conn = sqlite3.connect(str(db.DB_PATH))
    conn.execute(
        "INSERT INTO trades "
        "(strategy_id,strategy_name,symbol,direction,entry_ts,pnl_pct,"
        "exit_reason) VALUES (?,?,?,?,?,?,?)",
        ("orphan_id", "ghost_strategy", "BTC/USDT", "long",
         "2026-07-20T11:00:00", 0.5, "tp"),
    )
    conn.commit()
    conn.close()


def test_migration_collapses_fragmented_registry(forge_db):
    from forge.strategies import DEFAULT_STRATEGIES

    forge_db.init_db()
    _seed_fragmented(forge_db, DEFAULT_STRATEGIES, restarts=9)
    _register_orphan(forge_db)

    known = {s.id: s.to_dict() for s in DEFAULT_STRATEGIES}
    report = forge_db.cleanup_registry(known)

    assert report["registry_before"] == 144
    assert report["registry_deleted"] == 144
    assert report["trades_remapped"] == 144
    assert report["orphan_trades"] == 1

    conn = sqlite3.connect(str(forge_db.DB_PATH))
    conn.row_factory = sqlite3.Row
    # After seeding canonical rows separately, we should have exactly 16.
    for s in DEFAULT_STRATEGIES:
        forge_db.upsert_strategy(s.to_dict())
    assert conn.execute("SELECT COUNT(*) FROM strategy_registry").fetchone()[0] == 16
    # All 144 trades now hang off canonical ids (via strategy_name remap).
    canonical_ids = tuple(known.keys())
    placeholders = ",".join("?" * len(canonical_ids))
    n = conn.execute(
        f"SELECT COUNT(*) FROM trades WHERE strategy_id IN ({placeholders})",
        canonical_ids,
    ).fetchone()[0]
    assert n == 144
    # Orphan survived — not silently deleted.
    assert conn.execute(
        "SELECT COUNT(*) FROM trades WHERE strategy_id = ?", ("orphan_id",)
    ).fetchone()[0] == 1


def test_params_id_scrubbed(forge_db):
    """A stale `id` field inside the params JSON blob must be rewritten
    to match the PK id — otherwise hydration from the DB resurrects the
    old id, defeating the whole fix."""
    from forge.strategies import DEFAULT_STRATEGIES

    forge_db.init_db()
    _seed_fragmented(forge_db, DEFAULT_STRATEGIES, restarts=1)

    # After cleanup, upsert the canonical rows so params has a stable id
    # that may or may not match — we specifically test the scrub logic on
    # a row where params.id was written stale.
    conn = sqlite3.connect(str(forge_db.DB_PATH))
    conn.row_factory = sqlite3.Row
    row = conn.execute("SELECT id, params FROM strategy_registry LIMIT 1").fetchone()
    stale_id = row["id"]
    stale_params = json.loads(row["params"])
    assert stale_params["id"] == stale_id

    # Now force the PK id to differ from params.id (simulating a rewrite
    # where the id was updated but params drifted).
    conn.execute(
        "UPDATE strategy_registry SET id = ? WHERE id = ?",
        (stale_id + "_new", stale_id),
    )
    conn.commit(); conn.close()

    # cleanup with known set including the _new id
    forge_db.cleanup_registry({stale_id + "_new": {"name": stale_params["name"]}})

    conn = sqlite3.connect(str(forge_db.DB_PATH))
    conn.row_factory = sqlite3.Row
    r = conn.execute(
        "SELECT id, params FROM strategy_registry WHERE id = ?",
        (stale_id + "_new",),
    ).fetchone()
    assert r is not None
    assert json.loads(r["params"])["id"] == stale_id + "_new", (
        "params.id was not scrubbed to match the new PK id"
    )


def test_migration_is_idempotent(forge_db):
    from forge.strategies import DEFAULT_STRATEGIES

    forge_db.init_db()
    _seed_fragmented(forge_db, DEFAULT_STRATEGIES, restarts=9)
    known = {s.id: s.to_dict() for s in DEFAULT_STRATEGIES}

    r1 = forge_db.cleanup_registry(known)
    r2 = forge_db.cleanup_registry(known)
    r3 = forge_db.cleanup_registry(known)

    # First run does work; subsequent runs must be no-ops.
    assert r1["registry_deleted"] == 144
    assert r2["registry_deleted"] == 0
    assert r2["trades_remapped"] == 0
    assert r2["params_scrubbed"] == 0
    assert r3 == r2, "third run must be identical to second"


def test_wal_mode_enabled(forge_db):
    """WAL mode is required for the scorecard reader not to block candle
    inserts. If this fails, either the pragma got removed from init_db()
    or the DB was created without WAL and the file header still says
    DELETE — either way, the scorecard's aggregate query will serialize
    against writers."""
    forge_db.init_db()
    conn = sqlite3.connect(str(forge_db.DB_PATH))
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    conn.close()
    assert mode == "wal"


def test_schema_has_evaluation_history_and_scorecard(forge_db):
    """These tables MUST exist after init_db — the scorecard job depends
    on both. Regression against someone dropping the CREATE statements."""
    forge_db.init_db()
    conn = sqlite3.connect(str(forge_db.DB_PATH))
    tables = {
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    }
    conn.close()
    assert "strategy_scorecard" in tables
    assert "evaluation_history" in tables


def test_trades_has_model_version_columns(forge_db):
    """Nullable model_version + strategy_version columns must exist on
    trades. Cheap to add now, painful migration later — see db.py preamble."""
    forge_db.init_db()
    conn = sqlite3.connect(str(forge_db.DB_PATH))
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(trades)").fetchall()
            if hasattr(r, "keys")} or {r[1] for r in conn.execute("PRAGMA table_info(trades)").fetchall()}
    conn.close()
    assert "model_version" in cols
    assert "strategy_version" in cols
