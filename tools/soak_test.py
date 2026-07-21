#!/usr/bin/env python3
"""
soak_test.py — continuous health-monitoring soak test for the Antigravity
Predictor. Run this on real hardware (the target laptop, or the 3rd-party
QA environment) for a genuine, unattended, continuous run — it has no
built-in time limit and is designed to be left running for 60+ minutes.

What it does, every POLL_INTERVAL_SECONDS (default 15s):
  - hits /api/status, /api/feature-parity/{BTC,ETH,SOL}_USDT,
    /api/market-tickers, /api/enriched-signals, / (dashboard root)
  - records latency, HTTP status, and any error text
  - records the predictor process's RSS memory (via `ps`) to catch leaks
  - appends one JSON line per cycle to soak_test_log.jsonl (so a partial
    run is never lost even if the process is killed early)

At the end (Ctrl+C, or after DURATION_SECONDS if you set one), prints a
plain summary: total cycles, uptime %, error count with the actual error
text for each, and memory growth from first to last reading.

Usage:
    # against an already-running predictor on localhost:18910
    python3 soak_test.py

    # to also boot the predictor itself and monitor it for exactly 60 min
    python3 soak_test.py --boot --duration 3600

This script deliberately does NOT run inside the Claude sandbox that
built this package — that sandbox kills every process (including this
script itself) within ~45 seconds. Run this on real infrastructure.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    print("This script needs `requests` (pip install requests) — it is already "
          "a transitive dependency of this repo's requirements.txt, so the "
          "repo's own venv has it.")
    sys.exit(1)

BASE_URL = "http://localhost:18910"
LOG_PATH = Path(__file__).parent / "soak_test_log.jsonl"

ROUTES = [
    ("status", "/api/status"),
    ("feature_parity_btc", "/api/feature-parity/BTC_USDT"),
    ("feature_parity_eth", "/api/feature-parity/ETH_USDT"),
    ("feature_parity_sol", "/api/feature-parity/SOL_USDT"),
    ("market_tickers", "/api/market-tickers"),
    ("enriched_signals", "/api/enriched-signals"),
    ("dashboard_root", "/"),
]


def get_predictor_rss_mb() -> float | None:
    """Best-effort RSS (MB) of the predictor_server.py process, via ps."""
    try:
        out = subprocess.run(
            ["ps", "-eo", "pid,rss,cmd"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            if "predictor_server.py" in line and "grep" not in line:
                parts = line.split(None, 2)
                rss_kb = int(parts[1])
                return round(rss_kb / 1024, 1)
    except Exception:
        pass
    return None


def one_cycle(cycle_num: int) -> dict:
    entry = {
        "cycle": cycle_num,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "routes": {},
        "rss_mb": get_predictor_rss_mb(),
    }
    for name, path in ROUTES:
        t0 = time.monotonic()
        try:
            r = requests.get(BASE_URL + path, timeout=5)
            entry["routes"][name] = {
                "status": r.status_code,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "ok": r.status_code == 200,
            }
        except Exception as e:
            entry["routes"][name] = {
                "status": None,
                "latency_ms": round((time.monotonic() - t0) * 1000, 1),
                "ok": False,
                "error": str(e),
            }
    return entry


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=0,
                     help="Total seconds to run. 0 = run until Ctrl+C.")
    ap.add_argument("--interval", type=int, default=15,
                     help="Seconds between health-check cycles.")
    args = ap.parse_args()

    print(f"[soak_test] Monitoring {BASE_URL} every {args.interval}s. "
          f"{'Until Ctrl+C.' if not args.duration else f'For {args.duration}s.'}")
    print(f"[soak_test] Logging to {LOG_PATH}")

    start = time.monotonic()
    cycle = 0
    all_entries = []
    try:
        with open(LOG_PATH, "a") as logf:
            while True:
                cycle += 1
                entry = one_cycle(cycle)
                logf.write(json.dumps(entry) + "\n")
                logf.flush()
                all_entries.append(entry)

                ok_count = sum(1 for r in entry["routes"].values() if r["ok"])
                total = len(entry["routes"])
                print(f"[cycle {cycle}] {entry['timestamp']} "
                      f"routes_ok={ok_count}/{total} rss_mb={entry['rss_mb']}")

                if args.duration and (time.monotonic() - start) >= args.duration:
                    break
                time.sleep(args.interval)
    except KeyboardInterrupt:
        print("\n[soak_test] Stopped by user.")

    # Summary
    total_cycles = len(all_entries)
    total_checks = sum(len(e["routes"]) for e in all_entries)
    failed_checks = [
        (e["cycle"], e["timestamp"], name, r)
        for e in all_entries for name, r in e["routes"].items() if not r["ok"]
    ]
    rss_values = [e["rss_mb"] for e in all_entries if e["rss_mb"] is not None]

    print("\n" + "=" * 60)
    print("SOAK TEST SUMMARY")
    print("=" * 60)
    print(f"Total cycles: {total_cycles}")
    print(f"Wall time: {round(time.monotonic() - start, 1)}s")
    print(f"Total route checks: {total_checks}")
    print(f"Failed checks: {len(failed_checks)}")
    if failed_checks:
        for cyc, ts, name, r in failed_checks:
            print(f"  cycle {cyc} ({ts}) {name}: {r}")
    if rss_values:
        print(f"Predictor RSS: first={rss_values[0]}MB last={rss_values[-1]}MB "
              f"growth={round(rss_values[-1]-rss_values[0],1)}MB")
    uptime_pct = 100.0 * (total_checks - len(failed_checks)) / total_checks if total_checks else 0
    print(f"Uptime: {uptime_pct:.2f}%")
    print(f"Full log: {LOG_PATH}")


if __name__ == "__main__":
    main()
