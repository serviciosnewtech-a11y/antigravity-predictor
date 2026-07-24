#!/usr/bin/env python3
"""
tools/sync_backups_offsite.py -- push /opt/predictor-backups to an
off-host destination via an operator-configured command.

Why this exists (and why it's just a mechanism, not a destination):
DATA_INVENTORY.md flags row 19 as the single point where off-host sync
needs to land -- /opt/predictor-backups is already where every existing
backup script writes (signal_history, forge.db, configstate.tar.gz). But
the actual off-host destination is Luis's personal cloud accessed from the
VPS, which is still being developed. Shipping a hardcoded S3/B2/rclone/etc.
client here would either force a specific choice ahead of that decision or
box out whichever mechanism eventually gets picked. Instead, we ship the
scheduling + degradation logic and let OFFSITE_BACKUP_CMD be the pushing
seam:

    OFFSITE_BACKUP_CMD='rclone copy /opt/predictor-backups mydrive:predictor-backups'
    OFFSITE_BACKUP_CMD='aws s3 sync /opt/predictor-backups s3://my-bucket/predictor-backups'
    OFFSITE_BACKUP_CMD='rsync -av /opt/predictor-backups user@host:predictor-backups/'
    OFFSITE_BACKUP_CMD='azcopy sync /opt/predictor-backups https://my.blob.core.windows.net/predictor-backups'

If OFFSITE_BACKUP_CMD isn't set, this script exits 0 with a clear
"not configured, skipping" message -- the timer stays on and this stays
harmless. That's deliberate: sync_offsite.timer starts by default in
install.sh, but the service degrades gracefully when the env var is empty,
so it doesn't fail-loop against `systemctl status`. When Luis picks a
destination he sets OFFSITE_BACKUP_CMD in .env, restarts the service, and
the next timer tick pushes.

Command execution:
- Parsed with shlex.split() and executed via subprocess.run(argv, shell=False).
  Deliberately not shell=True: an operator-supplied string executed via
  the shell is a metacharacter-injection hazard even in a trusted context.
- BACKUP_DIR is passed to the command TWO ways: (1) appended as the final
  positional argv element (which is what rclone/aws/rsync/azcopy all
  expect for "source directory"), and (2) exported in the child process's
  environment as BACKUP_DIR so custom wrapper scripts can read it there
  too. Templates that embed BACKUP_DIR explicitly (e.g. `rsync -av
  /opt/predictor-backups host:dest/`) still work -- the appended arg is
  just the sync source repeated, harmless for these tools.

  Actually simpler and less surprising: just run the command as given,
  DON'T append BACKUP_DIR. Templates fully specify their source. The env
  var is available for custom scripts that want it. See design notes.

Exit codes:
- 0 when OFFSITE_BACKUP_CMD is unset or empty (service degrades gracefully)
- 0 when the wrapped command returns 0
- non-zero (the wrapped command's exit code) on real push failures

Env vars:
    BACKUP_DIR              Directory to push. Default /opt/predictor-backups.
    OFFSITE_BACKUP_CMD      Command line to invoke. Empty -> skip (exit 0).
"""
from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path


def _resolve_backup_dir() -> Path:
    return Path(os.environ.get("BACKUP_DIR") or "/opt/predictor-backups")


def run_once(backup_dir: Path, cmd_str: str) -> int:
    """Return the exit code the script itself should terminate with."""
    if not cmd_str or not cmd_str.strip():
        print("[sync_backups_offsite] OFFSITE_BACKUP_CMD is not configured -- "
              "off-host sync is a no-op today (this is expected until Luis "
              "picks a destination cloud; see tools/sync_backups_offsite.py "
              "docstring for example commands). Skipping.")
        return 0

    if not backup_dir.exists():
        print(f"[sync_backups_offsite] BACKUP_DIR {backup_dir} does not exist yet -- "
              f"nothing to push (expected on a fresh install before any of the "
              f"local backup timers has fired). Skipping.")
        return 0

    argv = shlex.split(cmd_str)
    if not argv:
        print("[sync_backups_offsite] OFFSITE_BACKUP_CMD parsed to zero args -- "
              "treating as unset. Skipping.")
        return 0

    # Pass BACKUP_DIR through env so custom wrapper scripts can read it.
    # Do NOT append it to argv: templates like `aws s3 sync <src> <dst>`
    # already fully specify their arguments; appending would corrupt them.
    env = os.environ.copy()
    env["BACKUP_DIR"] = str(backup_dir)

    print(f"[sync_backups_offsite] Pushing {backup_dir} via: {' '.join(argv)}")

    try:
        result = subprocess.run(
            argv,
            shell=False,  # argv-style, not shell-parsed -- injection-safe
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as e:
        print(f"[sync_backups_offsite] Command not found: {e}. "
              f"Verify the binary is installed and on the service's PATH "
              f"(systemd services don't inherit an interactive shell's PATH -- "
              f"use an absolute path in OFFSITE_BACKUP_CMD if in doubt).")
        return 127

    if result.stdout:
        print("[sync_backups_offsite] stdout:")
        print(result.stdout.rstrip())
    if result.stderr:
        print("[sync_backups_offsite] stderr:")
        print(result.stderr.rstrip())

    if result.returncode == 0:
        print(f"[sync_backups_offsite] Push succeeded.")
    else:
        print(f"[sync_backups_offsite] Push failed with exit code {result.returncode}.")

    return result.returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup-dir", type=Path, default=None,
                        help="Directory to push (default: $BACKUP_DIR or /opt/predictor-backups)")
    parser.add_argument("--cmd", type=str, default=None,
                        help="Override OFFSITE_BACKUP_CMD (for testing)")
    args = parser.parse_args()

    backup_dir = args.backup_dir or _resolve_backup_dir()
    cmd_str = args.cmd if args.cmd is not None else os.environ.get("OFFSITE_BACKUP_CMD", "")

    return run_once(backup_dir, cmd_str)


if __name__ == "__main__":
    sys.exit(main())
