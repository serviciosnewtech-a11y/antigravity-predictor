#!/usr/bin/env bash
# diagnose_gold.sh — one-shot diagnostic for the "gold not working" report
# on the bare-metal monolith deployment. Run this FROM the extracted
# antigravity-predictor-beta1.1 package directory, whether or not the
# predictor is already running.
#
# Paste the full output back — it answers the three questions needed to
# root-cause this: (1) is this actually the beta1.1 build, (2) does the
# backend serve real gold data, (3) what does the dashboard actually show.
set -uo pipefail

echo "════════════════════════════════════════════════════════"
echo "1. PACKAGE IDENTITY"
echo "════════════════════════════════════════════════════════"
echo "Current directory: $(pwd)"
if [ -f "../antigravity-predictor-beta1.1.tar.gz" ] || [ -f "./antigravity-predictor-beta1.1.tar.gz" ]; then
    for f in ../antigravity-predictor-beta1.1.tar.gz ./antigravity-predictor-beta1.1.tar.gz; do
        [ -f "$f" ] && echo "Found tarball: $f — sha256: $(sha256sum "$f" | awk '{print $1}')"
    done
    echo "Expected sha256: 1417ce5fb07441efe6c923d95bea5402ad0df9d43bcddf1174df3af00f56b289"
else
    echo "No .tar.gz found alongside this directory — can't confirm checksum this way."
fi
echo "Git commit baked into this checkout (if present):"
grep -o '"commit":[^,}]*' models/metadata.json 2>/dev/null || echo "  (not recorded in models/metadata.json)"
echo ""
echo "data/macro/ contents on disk right now:"
ls -la data/macro/ 2>&1
echo ""
echo "config.json macro-related keys (if any):"
grep -i "macro\|gold" config.json 2>&1 | head -10

echo ""
echo "════════════════════════════════════════════════════════"
echo "2. IS THE PREDICTOR RUNNING? BOOT IT IF NOT (30s test boot)"
echo "════════════════════════════════════════════════════════"
ALREADY_UP=0
if curl -s -m 3 -o /dev/null -w "" http://localhost:18910/api/status 2>/dev/null; then
    if curl -s -m 3 -o /dev/null -w "%{http_code}" http://localhost:18910/api/status | grep -q 200; then
        ALREADY_UP=1
        echo "Predictor already running on :18910 — using it as-is."
    fi
fi
if [ "$ALREADY_UP" -eq 0 ]; then
    echo "Not running — starting a test boot (will stay up after this script exits; stop manually if undesired)…"
    export SA_INFERENCE_BACKEND=disabled
    nohup .venv/bin/python src/predictor_server.py > /tmp/diag_predictor.log 2>&1 &
    for i in $(seq 1 20); do
        curl -s -o /dev/null -w "" http://localhost:18910/api/status 2>/dev/null && \
        curl -s -o /dev/null -w "%{http_code}" http://localhost:18910/api/status 2>/dev/null | grep -q 200 && break
        sleep 1
    done
    echo "Boot log tail:"
    tail -20 /tmp/diag_predictor.log
fi

echo ""
echo "════════════════════════════════════════════════════════"
echo "3. WHAT DOES THE BACKEND ACTUALLY SERVE FOR GOLD?"
echo "════════════════════════════════════════════════════════"
echo "-- /api/status --"
curl -s -m 5 -w "\nHTTP %{http_code}\n" http://localhost:18910/api/status
echo ""
echo "-- /api/market-tickers (full response) --"
curl -s -m 5 -w "\nHTTP %{http_code}\n" http://localhost:18910/api/market-tickers
echo ""
echo "-- /api/market-tickers, XAU/USD entry only --"
curl -s -m 5 http://localhost:18910/api/market-tickers | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    xau = [a for a in d.get('assets', []) if a.get('symbol') == 'XAU/USD']
    print(xau if xau else 'XAU/USD NOT PRESENT IN RESPONSE — this is the actual bug if so')
except Exception as e:
    print('Could not parse response as JSON:', e)
"

echo ""
echo "════════════════════════════════════════════════════════"
echo "4. FRONTEND — does the dashboard HTML/JS even reference the fix?"
echo "════════════════════════════════════════════════════════"
grep -c "wl-price-XAU-USD" dashboard/app.js 2>&1
grep -c "advisory-agent-bubble" dashboard/index.html 2>&1
echo "(both should print 1 or more — if they print 0, this is an OLDER package, not beta1.1)"

echo ""
echo "════════════════════════════════════════════════════════"
echo "DONE. Paste all of the above back — it's enough to pinpoint"
echo "whether this is a stale/old package, a backend bug, or a"
echo "frontend rendering issue specific to this machine's browser."
echo "════════════════════════════════════════════════════════"
