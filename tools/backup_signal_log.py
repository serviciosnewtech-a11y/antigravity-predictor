#!/usr/bin/env python3
"""
tools/backup_signal_log.py — periodic durable backup of signal_history.db.

Why this exists: signal_history.db (src/signal_log.py) is the ONLY durable
record of every signal transition and completed trade the predictor has
ever produced -- restart-safe by design (see signal_log.py's own docstring),
but that only protects against process restarts, not against losing the
whole host: a bad `rm -rf`, a disk failure, or (the actual live incident
that prompted this) simply standing up a fresh install on a different
machine with no path to bring the old data along. A single copy of a
SQLite file living inside the same app directory that install.sh/deploy
tooling touches is not "preserved at all costs" -- it's one accident away
from gone. Found and fixed live 2026-07-23.

What this does: takes a safe, consistent snapshot of signal_history.db
using SQLite's own online backup API (NOT a raw file copy -- a plain `cp`
of a SQLite file that's open elsewhere can capture a mid-write, corrupted
state; sqlite3.Connection.backup() is explicitly designed to be safe to run
against a live, in-use database) into a directory OUTSIDE the app's own
directory tree, on the same principle as releases/ living outside the git
repo: build output (and here, generated data) that needs to survive
operations performed ON the app directory shouldn't live inside it.
Old backups beyond BACKUP_RETENTION_COUNT are pruned so this doesn't grow
unbounded forever.

This is local-disk redundancy, not off-host/off-site backup -- it protects
against "the app directory got wiped/corrupted" and "one bad reinstall,"
not against "the whole host/disk is gone." True off-host durability (sync
to remote storage, another host, etc.) is a real, separate piece of work,
not yet decided/built -- flagged, not solved, here.

Usage:
    python3 tools/backup_signal_log.py [--source PATH] [--dest-dir PATH] [--keep N]

Env vars (used by the systemd timer, see predictor_backup.service):
    LOGS_DIR              Where signal_history.db actually lives (same var
                           signal_log.py itself reads). Default: ../logs
                           relative to this file's location.
    BACKUP_DIR             Where to write backups. Default: a sibling
                           "predictor-backups" directory next to LOGS_DIR's
                           parent (i.e. outside the app dir entirely on a
                           standard bare-metal install: LOGS_DIR is
                           /opt/predictor/logs, so the default backup dir is
                           /opt/predictor-backups).
    BACKUP_RETENTION_COUNT Number of most-recent backups to keep. Default 30.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


def _default_source() -> Path:
    logs_dir = Path(
        os.environ.get("LOGS_DIR")
        or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs")
    )
    return logs_dir / "signal_history.db"


def _default_dest_dir(source: Path) -> Path:
    env = os.environ.get("BACKUP_DIR")
    if env:
        return Path(env)
    # source is normally .../opt/predictor/logs/signal_history.db -- back up
    # two levels (out of logs/, out of the app dir) so the default lands
    # outside /opt/predictor entirely, not just in a different subdirectory
    # of the same tree that a future `rm -rf $APP_DIR` would still destroy.
    app_dir = source.parent.parent
    return app_dir.parent / f"{app_dir.name}-backups"


def backup_once(source: Path, dest_dir: Path, keep: int) -> Path | None:
    if not source.exists():
        print(f"[backup_signal_log] {source} does not exist yet -- nothing to back up "
              f"(expected on a fresh install with no signals/trades recorded yet).")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond precision: two invocations landing in the same second
    # (manual re-runs, tests) would otherwise collide on filename and
    # silently overwrite each other instead of producing two distinct
    # snapshots -- found via the test for this script itself.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000:06d}"
    dest_path = dest_dir / f"signal_history.{stamp}.db"

    # sqlite3's backup API, not a raw file copy -- safe against a live,
    # in-use database (predictor.service/signal_agent.service may be
    # writing to the source concurrently).
    src_conn = sqlite3.connect(str(source))
    dest_conn = sqlite3.connect(str(dest_path))
    try:
        with dest_conn:
            src_conn.backup(dest_conn)
    finally:
        src_conn.close()
        dest_conn.close()

    print(f"[backup_signal_log] Backed up {source} -> {dest_path} "
          f"({dest_path.stat().st_size} bytes).")

    _prune_old_backups(dest_dir, keep)
    return dest_path


def _prune_old_backups(dest_dir: Path, keep: int) -> None:
    backups = sorted(dest_dir.glob("signal_history.*.db"))
    if len(backups) <= keep:
        return
    to_remove = backups[:-keep]
    for old in to_remove:
        old.unlink()
        print(f"[backup_signal_log] Pruned old backup: {old}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=None,
                         help="Path to signal_history.db (default: $LOGS_DIR/signal_history.db)")
    parser.add_argument("--dest-dir", type=Path, default=None,
                         help="Directory to write timestamped backups into")
    parser.add_argument("--keep", type=int,
                         default=int(os.environ.get("BACKUP_RETENTION_COUNT", "30")),
                         help="Number of most-recent backups to retain (default: 30)")
    args = parser.parse_args()

    source = args.source or _default_source()
    dest_dir = args.dest_dir or _default_dest_dir(source)

    backup_once(source, dest_dir, args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
