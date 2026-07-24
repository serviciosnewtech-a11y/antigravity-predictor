"""
tests/test_backup_forge_db.py — regression test for tools/backup_forge_db.py,
added beta-1.10.16 after §7.10's scorecard/evaluation_history work made
forge.db carry evaluation trajectory worth preserving.

Same shape as test_backup_signal_log.py. Extra assertion: the forge
retention pass must NOT touch signal_history.*.db files even though both
DB backup families share the same target directory by design.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import backup_forge_db as bfd  # noqa: E402


def _make_source_db(path: Path, rows=(("btc_baseline", 0.5),)):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE trades (id INTEGER PRIMARY KEY, strategy_id TEXT, pnl_pct REAL)"
    )
    conn.executemany(
        "INSERT INTO trades (strategy_id, pnl_pct) VALUES (?,?)", rows
    )
    conn.commit()
    conn.close()


def test_backup_preserves_data_via_sqlite_backup_api(tmp_path):
    src = tmp_path / "forge_data" / "forge.db"
    _make_source_db(src, rows=(("btc_baseline", 0.5), ("sol_hi_conf", -1.2)))
    dest_dir = tmp_path / "backups"

    result = bfd.backup_once(src, dest_dir, keep=30)

    assert result is not None and result.exists()
    conn = sqlite3.connect(str(result))
    rows = conn.execute(
        "SELECT strategy_id, pnl_pct FROM trades ORDER BY id"
    ).fetchall()
    conn.close()
    assert rows == [("btc_baseline", 0.5), ("sol_hi_conf", -1.2)]


def test_repeated_backups_produce_distinct_files_not_overwrites(tmp_path):
    src = tmp_path / "forge_data" / "forge.db"
    _make_source_db(src)
    dest_dir = tmp_path / "backups"

    paths = [bfd.backup_once(src, dest_dir, keep=30) for _ in range(3)]

    assert len(set(paths)) == 3
    for p in paths:
        assert p.exists()


def test_retention_prunes_only_forge_backups(tmp_path):
    """Both backup families share the target directory. The forge retention
    pass must prune only forge.*.db files -- never signal_history.*.db --
    or the two backup jobs would delete each other's snapshots on every run."""
    src = tmp_path / "forge_data" / "forge.db"
    _make_source_db(src)
    dest_dir = tmp_path / "backups"
    dest_dir.mkdir()

    # Plant three pre-existing signal_history backups (as if from
    # tools/backup_signal_log.py runs) that must survive intact.
    for i in range(3):
        (dest_dir / f"signal_history.2026070{i}-000000-000000.db").write_bytes(
            b"pretend signal history " + str(i).encode()
        )

    # Now run 5 forge backups with keep=2 → only 2 forge.*.db should remain,
    # all 3 signal_history.*.db files untouched.
    for _ in range(5):
        bfd.backup_once(src, dest_dir, keep=2)

    forge_remaining = sorted(dest_dir.glob("forge.*.db"))
    signal_remaining = sorted(dest_dir.glob("signal_history.*.db"))
    assert len(forge_remaining) == 2, forge_remaining
    assert len(signal_remaining) == 3, (
        f"forge retention pass pruned signal_history backups: {signal_remaining}"
    )


def test_missing_source_does_not_raise(tmp_path):
    missing = tmp_path / "forge_data" / "forge.db"  # never created
    dest_dir = tmp_path / "backups"

    result = bfd.backup_once(missing, dest_dir, keep=30)

    assert result is None
    assert not dest_dir.exists() or not list(dest_dir.glob("*.db"))


def test_default_dest_dir_matches_signal_history_backup_dir():
    """Both backup jobs target the same directory by design -- one place
    to look for all durable snapshots, one directory to point off-host
    sync at when that becomes real. Regression against the two ever
    silently diverging."""
    source = Path("/opt/predictor/forge_data/forge.db")
    dest = bfd._default_dest_dir(source)
    assert str(dest) == "/opt/predictor-backups", dest
    assert not str(dest).startswith("/opt/predictor/"), (
        f"default backup dir {dest} is still inside the app directory it's "
        f"supposed to protect against"
    )
