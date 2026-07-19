#!/usr/bin/env python3
"""
tools/admin_chat.py — terminal client for the personal admin agent.

This is intentionally NOT part of the dashboard UI — the admin agent has
unrestricted shell access on your machine, gated only by ADMIN_API_TOKEN,
and is meant to be reached only by you, from a terminal, on this box (or
your LAN).

Usage:
    ADMIN_API_TOKEN=... python3 tools/admin_chat.py
    python3 tools/admin_chat.py --url http://localhost:18913 --token ...
"""
from __future__ import annotations

import argparse
import os
import sys

import requests


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default=os.environ.get("ADMIN_AGENT_URL", "http://localhost:18913"))
    parser.add_argument("--token", default=os.environ.get("ADMIN_API_TOKEN", ""))
    args = parser.parse_args()

    if not args.token:
        print("ERROR: no admin token. Set ADMIN_API_TOKEN or pass --token.", file=sys.stderr)
        return 1

    print(f"Admin agent client — {args.url}")
    print("This agent has UNRESTRICTED shell access on the host. Type 'exit' to quit.\n")

    history = []
    while True:
        try:
            msg = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not msg:
            continue
        if msg.lower() in ("exit", "quit"):
            return 0

        try:
            resp = requests.post(
                f"{args.url}/admin-chat",
                json={"message": msg, "history": history},
                headers={"X-Admin-Token": args.token},
                timeout=180,
            )
        except Exception as e:
            print(f"[connection error: {e}]")
            continue

        if resp.status_code != 200:
            print(f"[error {resp.status_code}: {resp.text[:300]}]")
            continue

        data = resp.json()
        for cmd in data.get("commands_run", []):
            print(f"  $ {cmd['command']}")
            out = cmd["output"]
            print("  " + out.replace("\n", "\n  "))
        print(f"agent> {data.get('reply', '')}\n")

        history.append({"role": "user", "content": msg})
        history.append({"role": "assistant", "content": data.get("reply", "")})
        history = history[-16:]


if __name__ == "__main__":
    raise SystemExit(main())
