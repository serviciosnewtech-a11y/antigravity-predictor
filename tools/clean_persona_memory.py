#!/usr/bin/env python3
"""
tools/clean_persona_memory.py — purge relay-error pollution from persona
memory logs, without wiping legitimate conversation history.

Background: before this fix, tools/agent_chat_relay.py returned HTTP 200
with error text embedded in the "reply" field whenever the underlying
agent invocation actually failed (e.g. a missing CLI profile). predictor_
server.py had no way to tell that apart from a real reply, so it got
written into logs/crypto_operator_memory.jsonl as if it were a genuine
conversation turn — meaning it would keep getting recalled into every
future chat's context. The relay itself is now fixed to return a real
error status instead, so this stops happening going forward — but this
script is for cleaning up memory files that already have that pollution
in them from before the fix.

(Historical note: this used to also clean logs/tutor_memory.jsonl, from
when the dashboard had a separate "Hermes Tutor" persona/endpoint with its
own memory file. That was merged back into the one operator persona
2026-07-23, so there's only one memory file to clean now.)

Usage:
    python3 tools/clean_persona_memory.py                 # dry run, reports what would be removed
    python3 tools/clean_persona_memory.py --apply          # actually rewrites the files
    python3 tools/clean_persona_memory.py --apply --backup # also writes a .bak copy first
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path

_ERROR_MARKERS = (
    "[agent_relay]",
    "[metis_relay]",
    "agent_invocation_failed",
)


def _is_polluted(rec: dict) -> bool:
    agent_text = str(rec.get("agent", ""))
    lowered = agent_text.lower()
    if any(marker.lower() in lowered for marker in _ERROR_MARKERS):
        return True
    if lowered.startswith(("error:", "error ", "fatal:", "traceback")):
        return True
    return False


def clean_file(path: Path, apply: bool, backup: bool) -> tuple[int, int]:
    if not path.exists():
        print(f"  {path}: does not exist, skipping")
        return 0, 0
    lines = path.read_text().strip().splitlines()
    kept, removed = [], 0
    for line in lines:
        try:
            rec = json.loads(line)
        except Exception:
            kept.append(line)  # leave unparseable lines alone, don't guess
            continue
        if _is_polluted(rec):
            removed += 1
        else:
            kept.append(line)

    print(f"  {path}: {len(lines)} total, {removed} polluted, {len(kept)} kept")
    if apply and removed > 0:
        if backup:
            shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
            print(f"    backup written: {path.with_suffix(path.suffix + '.bak')}")
        path.write_text("\n".join(kept) + ("\n" if kept else ""))
        print("    rewritten.")
    return len(lines), removed


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Actually rewrite the files (default: dry run).")
    parser.add_argument("--backup", action="store_true", help="With --apply, write a .bak copy of each file first.")
    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parent.parent
    logs_dir = repo_root / "logs"
    targets = [
        logs_dir / "crypto_operator_memory.jsonl",
    ]

    print(f"{'APPLYING' if args.apply else 'DRY RUN'} — scanning persona memory files:")
    for path in targets:
        clean_file(path, args.apply, args.backup)

    if not args.apply:
        print("\nDry run only — re-run with --apply to actually rewrite the files.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
