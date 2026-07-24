#!/usr/bin/env python3
"""
tools/backup_forge_db.py — periodic durable backup of forge_data/forge.db.

Why this exists: forge_data/forge.db is the only durable record of every
paper trade Forge has ever simulated PLUS (as of beta-1.10.15) the entire
strategy_scorecard + evaluation_history trajectory that the scoring loop
writes. It was originally out of scope for tools/backup_signal_log.py
because Forge had no scoring artifact worth preserving -- the runtime was
"just" a paper trader whose value was in the process, not the accumulated
data. §7.10 changed that: `evaluation_history` is now append-only and its
depth is the entire substrate for future trend-based verdicts
("recovering", "degrading" -- deferred to v2). Losing it means losing the
runway toward those verdicts.

Same pattern as tools/backup_signal_log.py -- SQLite backup-API snapshots
(NOT a raw file copy, which can capture a mid-write corrupted state on a
live in-use DB) into a directory OUTSIDE the app's own directory tree
(defaults to the SAME /opt/predictor-backups directory as
signal_history.db backups, distinguished by filename prefix -- one place
to look for all durable snapshots, one directory to point off-host sync at
when that becomes a real thing).

Local-disk redundancy only, not off-host/off-site. See backup_signal_log.py
docstring for the same caveat -- true off-host durability is separate work.

Usage:
    python3 tools/backup_forge_db.py [--source PATH] [--dest-dir PATH] [--keep N]

Env vars (used by forge_backup.service):
    FORGE_DATA_DIR              Where forge.db lives (same var forge/db.py
                                 reads). Default: forge_data (relative).
    BACKUP_DIR                   Where to write backups. Default: same as
                                 backup_signal_log.py -- a sibling
                                 "<app>-backups" directory outside the app
                                 dir entirely (typically /opt/predictor-
                                 backups on a standard bare-metal install).
    FORGE_BACKUP_RETENTION_COUNT Number of most-recent backups to keep.
                                 Default 30. Distinct from
                                 BACKUP_RETENTION_COUNT to allow independent
                                 tuning (forge.db grows differently from
                                 signal_history.db and may warrant a
                                 different retention).
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from pathlib import Path


def _default_source() -> Path:
    data_dir = Path(
        os.environ.get("FORGE_DATA_DIR")
        or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "forge_data")
    )
    return data_dir / "forge.db"


def _default_dest_dir(source: Path) -> Path:
    """Same default target directory as tools/backup_signal_log.py. One
    place to look for all durable snapshots; filenames self-identify which
    DB they came from (signal_history.*.db vs forge.*.db)."""
    env = os.environ.get("BACKUP_DIR")
    if env:
        return Path(env)
    # source is normally .../opt/predictor/forge_data/forge.db -- back up
    # two levels (out of forge_data/, out of the app dir) so the default
    # lands outside /opt/predictor entirely.
    app_dir = source.parent.parent
    return app_dir.parent / f"{app_dir.name}-backups"


def backup_once(source: Path, dest_dir: Path, keep: int) -> Path | None:
    if not source.exists():
        print(f"[backup_forge_db] {source} does not exist yet -- nothing to back up "
              f"(expected on a fresh install where forge.service has never started).")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    # Microsecond precision on the stamp -- two invocations in the same
    # second (manual re-runs, tests) would otherwise collide on filename
    # and silently overwrite each other. Same pattern as backup_signal_log.
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000:06d}"
    dest_path = dest_dir / f"forge.{stamp}.db"

    src_conn = sqlite3.connect(str(source))
    dest_conn = sqlite3.connect(str(dest_path))
    try:
        with dest_conn:
            src_conn.backup(dest_conn)
    finally:
        src_conn.close()
        dest_conn.close()

    print(f"[backup_forge_db] Backed up {source} -> {dest_path} "
          f"({dest_path.stat().st_size} bytes).")

    _prune_old_backups(dest_dir, keep)
    return dest_path


def _prune_old_backups(dest_dir: Path, keep: int) -> None:
    """Prune only forge.* backups; leaves signal_history.* backups alone
    (they share this directory but each has its own retention pass)."""
    backups = sorted(dest_dir.glob("forge.*.db"))
    if len(backups) <= keep:
        return
    to_remove = backups[:-keep]
    for old in to_remove:
        old.unlink()
        print(f"[backup_forge_db] Pruned old backup: {old}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=None,
                         help="Path to forge.db (default: $FORGE_DATA_DIR/forge.db)")
    parser.add_argument("--dest-dir", type=Path, default=None,
                         help="Directory to write timestamped backups into")
    parser.add_argument("--keep", type=int,
                         default=int(os.environ.get("FORGE_BACKUP_RETENTION_COUNT", "30")),
                         help="Number of most-recent forge backups to retain (default: 30)")
    args = parser.parse_args()

    source = args.source or _default_source()
    dest_dir = args.dest_dir or _default_dest_dir(source)

    backup_once(source, dest_dir, args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
