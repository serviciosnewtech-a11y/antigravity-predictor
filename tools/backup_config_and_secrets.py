#!/usr/bin/env python3
"""
tools/backup_config_and_secrets.py -- periodic durable backup of the
"unprotected" config + secrets + persona-memory + model sources called out
as coverage="none" in docs/DATA_INVENTORY.md's summary table (rows 4, 5, 6,
7, 8, 9, 10).

Why this exists: signal_history.db and forge.db have their own 6h backup
timers (tools/backup_signal_log.py, tools/backup_forge_db.py), but every
other durable source on the host has zero backup coverage today. This
ships local-disk redundancy for those, into the same /opt/predictor-backups
directory the SQLite backups already land in (one place to look for all
durable snapshots, one directory to eventually point off-host sync at --
see tag beta-1.10.22 for the off-host stub).

What lands in the tarball (all optional -- missing sources are logged and
skipped, they don't fail the whole run):
  - .env                                       (DATA_INVENTORY row 9)
  - /etc/nginx/.htpasswd                       (row 10)
  - logs/crypto_operator_memory.jsonl          (row 4 -- persona memory)
  - config.json                                (row 6)
  - models/model_*.txt                         (row 5 -- production models)
  - models/*.json                              (row 7 -- metadata, metrics,
                                                reports)

Why one tarball for all of them, not one file per source: they're small,
they change together (a retrain rewrites models + metadata + report;
recalibrate rewrites config + report; install rotates .env + .htpasswd),
and restoring them piecewise is more error-prone than restoring them as a
consistent set. `configstate.<stamp>.tar.gz` is the filename convention;
retention is scoped to that glob so the pass never touches the sibling
signal_history.*.db / forge.*.db snapshots.

This is LOCAL-disk redundancy only. Same caveat as the other two backup
scripts: this protects against a bad reinstall or an accidental `rm -rf`
of the app dir, NOT against losing the whole host/disk. Off-host handling
is separate work (see tag beta-1.10.22 for the mechanism, off-host
destination is Luis's personal cloud accessed from the VPS, still being
developed).

Usage:
    python3 tools/backup_config_and_secrets.py \
        [--app-dir PATH] [--dest-dir PATH] [--htpasswd PATH] [--keep N]

Env vars (used by config_backup.service):
    APP_DIR                              Root of the app install.
                                         Default: parent dir of tools/.
    BACKUP_DIR                           Where to write backups. Default:
                                         same as backup_signal_log.py --
                                         /opt/predictor-backups on a
                                         standard bare-metal install.
    HTPASSWD_PATH                        Nginx htpasswd file (may be
                                         outside APP_DIR). Default:
                                         /etc/nginx/.htpasswd.
    CONFIGSTATE_BACKUP_RETENTION_COUNT   Number of most-recent
                                         configstate.*.tar.gz snapshots
                                         to keep. Default 30 (independent
                                         of the two SQLite retentions; a
                                         configstate tarball is small and
                                         changes less often -- a longer
                                         retention here is cheap).
"""
from __future__ import annotations

import argparse
import os
import sys
import tarfile
import time
from pathlib import Path
from typing import Iterable


def _default_app_dir() -> Path:
    return Path(
        os.environ.get("APP_DIR")
        or os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
    ).resolve()


def _default_dest_dir(app_dir: Path) -> Path:
    """Same target as tools/backup_signal_log.py / backup_forge_db.py --
    one place to look for all durable snapshots. See those scripts'
    docstrings for the reasoning (in short: outside the app dir so a
    reinstall/wipe of the app dir doesn't take the backups with it)."""
    env = os.environ.get("BACKUP_DIR")
    if env:
        return Path(env)
    # app_dir is normally /opt/predictor -- sibling dir /opt/predictor-backups.
    return app_dir.parent / f"{app_dir.name}-backups"


def _default_htpasswd() -> Path:
    return Path(os.environ.get("HTPASSWD_PATH") or "/etc/nginx/.htpasswd")


def _collect_sources(app_dir: Path, htpasswd: Path) -> list[tuple[Path, str]]:
    """Return (real_path, name_in_tarball) pairs for every source that
    exists. Missing sources are silently skipped -- the caller logs them so
    an operator can spot a source that never appeared where expected.
    name_in_tarball is deliberately flat-ish (no absolute paths) so a
    restore tool can drop the tarball anywhere without leaking the source
    machine's exact layout."""
    candidates: list[tuple[Path, str]] = []

    # Row 9 -- .env
    env_path = app_dir / ".env"
    candidates.append((env_path, ".env"))

    # Row 10 -- /etc/nginx/.htpasswd (deliberately NOT under app_dir)
    candidates.append((htpasswd, "etc/nginx/.htpasswd"))

    # Row 4 -- persona memory
    candidates.append(
        (app_dir / "logs" / "crypto_operator_memory.jsonl",
         "logs/crypto_operator_memory.jsonl")
    )

    # Row 6 -- config.json (canonical + src/ copy that install.sh writes)
    candidates.append((app_dir / "config.json", "config.json"))
    candidates.append((app_dir / "src" / "config.json", "src/config.json"))

    # Row 5 -- production models. Enumerate at call time (glob) so we
    # capture whatever's actually on disk right now, not a hardcoded list.
    models_dir = app_dir / "models"
    for p in sorted(models_dir.glob("model_*.txt")):
        candidates.append((p, f"models/{p.name}"))

    # Row 7 -- model metadata / metrics / reports.
    for p in sorted(models_dir.glob("*.json")):
        candidates.append((p, f"models/{p.name}"))

    # Filter to what actually exists (missing sources are OK).
    return [(src, name) for (src, name) in candidates if src.exists()]


def backup_once(app_dir: Path, dest_dir: Path, htpasswd: Path, keep: int) -> Path | None:
    sources = _collect_sources(app_dir, htpasswd)
    if not sources:
        print(f"[backup_config_and_secrets] No sources found under {app_dir} "
              f"(and no {htpasswd}) -- nothing to back up. "
              f"Expected on a fresh install where install.sh hasn't run yet.")
        return None

    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime()) + f"-{time.time_ns() % 1_000_000:06d}"
    dest_path = dest_dir / f"configstate.{stamp}.tar.gz"

    with tarfile.open(dest_path, "w:gz") as tar:
        for src, arcname in sources:
            tar.add(str(src), arcname=arcname, recursive=False)

    total = sum(1 for _ in sources)
    print(f"[backup_config_and_secrets] Bundled {total} source(s) into {dest_path} "
          f"({dest_path.stat().st_size} bytes).")
    for src, arcname in sources:
        print(f"[backup_config_and_secrets]   {arcname} <- {src}")

    _prune_old_backups(dest_dir, keep)
    return dest_path


def _prune_old_backups(dest_dir: Path, keep: int) -> None:
    """Prune only configstate.*.tar.gz -- never signal_history.*.db or
    forge.*.db, which share the target directory by design."""
    backups = sorted(dest_dir.glob("configstate.*.tar.gz"))
    if len(backups) <= keep:
        return
    to_remove = backups[:-keep]
    for old in to_remove:
        old.unlink()
        print(f"[backup_config_and_secrets] Pruned old backup: {old}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app-dir", type=Path, default=None,
                        help="App root (default: parent of tools/, or $APP_DIR)")
    parser.add_argument("--dest-dir", type=Path, default=None,
                        help="Directory to write timestamped tarballs into")
    parser.add_argument("--htpasswd", type=Path, default=None,
                        help="Nginx htpasswd file (default: /etc/nginx/.htpasswd)")
    parser.add_argument("--keep", type=int,
                        default=int(os.environ.get("CONFIGSTATE_BACKUP_RETENTION_COUNT", "30")),
                        help="Number of most-recent configstate backups to retain (default: 30)")
    args = parser.parse_args()

    app_dir = args.app_dir or _default_app_dir()
    dest_dir = args.dest_dir or _default_dest_dir(app_dir)
    htpasswd = args.htpasswd or _default_htpasswd()

    backup_once(app_dir, dest_dir, htpasswd, args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
