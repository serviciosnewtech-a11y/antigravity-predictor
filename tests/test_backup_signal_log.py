"""
tests/test_backup_signal_log.py — regression test for tools/backup_signal_log.py,
added 2026-07-23 after a live rehearsal deploy came up with none of the prior
host's trade/signal history: signal_history.db (src/signal_log.py) is restart-
safe, but a single copy living inside the app directory tree is not safe
against losing the whole host, a bad reinstall, or (the actual incident) just
standing up on different hardware with no path to carry old data forward.

Covers: a safe (SQLite backup-API, not raw file copy) snapshot actually
preserves the data, repeated runs produce distinct timestamped files rather
than colliding/overwriting each other, retention pruning keeps exactly the
configured number of most-recent backups, and a missing source (fresh
install, nothing recorded yet) is handled without raising.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

import backup_signal_log as bsl  # noqa: E402


def _make_source_db(path: Path, rows=(("BTC/USDT",),)):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE trades (id INTEGER PRIMARY KEY, symbol TEXT)")
    conn.executemany("INSERT INTO trades (symbol) VALUES (?)", rows)
    conn.commit()
    conn.close()


def test_backup_preserves_data_via_sqlite_backup_api(tmp_path):
    src = tmp_path / "logs" / "signal_history.db"
    _make_source_db(src, rows=(("BTC/USDT",), ("ETH/USDT",)))
    dest_dir = tmp_path / "backups"

    result = bsl.backup_once(src, dest_dir, keep=30)

    assert result is not None and result.exists()
    conn = sqlite3.connect(str(result))
    rows = conn.execute("SELECT symbol FROM trades ORDER BY id").fetchall()
    conn.close()
    assert rows == [("BTC/USDT",), ("ETH/USDT",)]


def test_repeated_backups_produce_distinct_files_not_overwrites(tmp_path):
    src = tmp_path / "logs" / "signal_history.db"
    _make_source_db(src)
    dest_dir = tmp_path / "backups"

    paths = [bsl.backup_once(src, dest_dir, keep=30) for _ in range(3)]

    assert len(set(paths)) == 3, (
        "three separate backup_once() calls produced fewer than 3 distinct "
        "filenames -- this was a real bug: a second-precision timestamp let "
        "rapid successive runs collide and silently overwrite each other"
    )
    for p in paths:
        assert p.exists()


def test_retention_prunes_to_configured_count(tmp_path):
    src = tmp_path / "logs" / "signal_history.db"
    _make_source_db(src)
    dest_dir = tmp_path / "backups"

    for _ in range(5):
        bsl.backup_once(src, dest_dir, keep=2)

    remaining = sorted(dest_dir.glob("signal_history.*.db"))
    assert len(remaining) == 2, f"expected exactly 2 backups retained, got {len(remaining)}"


def test_missing_source_does_not_raise(tmp_path):
    missing = tmp_path / "logs" / "signal_history.db"  # never created
    dest_dir = tmp_path / "backups"

    result = bsl.backup_once(missing, dest_dir, keep=30)

    assert result is None
    assert not dest_dir.exists() or not list(dest_dir.glob("*.db"))


def test_default_dest_dir_lands_outside_app_directory():
    # Standard bare-metal layout: LOGS_DIR=/opt/predictor/logs -- the default
    # backup destination must NOT be anywhere under /opt/predictor, since the
    # whole point is surviving operations performed on that directory.
    source = Path("/opt/predictor/logs/signal_history.db")
    dest = bsl._default_dest_dir(source)
    assert str(dest) == "/opt/predictor-backups", dest
    assert not str(dest).startswith("/opt/predictor/"), (
        f"default backup dir {dest} is still inside the app directory it's "
        f"supposed to protect against"
    )
