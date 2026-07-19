# Beta 1.1 Logging/Reporting Implementation Plan

**Status:** planned, not implemented in Beta 1.  
**Scope:** dry-run/paper evaluation logging only. No live trading, no exchange credentials, no autonomous promotion.

## Intent

Adopt the attached parallel-strategy logging/reporting design without misrepresenting current Beta 1 behavior.

Beta 1 currently provides:

- predictor WebSocket + REST status
- predictor in-memory paper trades exposed through `/api/trades`
- Forge SQLite strategy evaluation
- dry-run executor
- optional disabled-by-default signal-agent enrichment

Beta 1 does **not** yet provide append-only JSONL logs or mechanical weekly paper reports.

## Implementation order

1. **Stable strategy identity**
   - Add stable strategy IDs for default Forge strategies.
   - Register `main_beta1` as the predictor baseline.
   - Keep SOL long disabled.

2. **Separate writer paths**
   - Avoid shared JSONL file handles across services.
   - Use service-owned paths:

```text
logs/predictor/signals/YYYY-MM-DD.jsonl
logs/predictor/trades/YYYY-MM-DD.jsonl
logs/forge/registry/strategies.jsonl
logs/forge/signals/YYYY-MM-DD.jsonl
logs/forge/trades/YYYY-MM-DD.jsonl
logs/system/YYYY-MM-DD.jsonl
reports/paper/YYYY-Www.md
```

3. **Cost-normalized paper PnL**
   - Apply one explicit cost model before reporting expectancy:
     - `round_trip_pct=0.0015`
     - BTC/ETH spread offset `0.0002`
     - SOL spread offset `0.0003`
     - reference notional `100 USDT`
   - Record both gross and net PnL.

4. **System/feature health records**
   - Heartbeat records.
   - WebSocket reconnect records.
   - Feature-null/degraded records.
   - Define or remove the `H-13` label before implementation.

5. **Weekly report compiler**
   - Read JSONL logs only.
   - Write reports under `reports/paper/`.
   - Reports may be committed; raw runtime logs remain gitignored.

6. **Promotion discipline**
   - No mid-window tuning.
   - Variant parameter changes require a new `strategy_id`.
   - A winning variant must re-prove itself in a fresh confirmation window as the only candidate before promotion.

## Verification gates

Before Beta 1.1 can be called implemented:

```bash
python3 -m py_compile executor/server.py src/predictor_server.py src/signal_agent/config.py src/signal_agent/main.py forge/*.py
bash -n deploy.sh diagnose.sh deploy_target_smoke.sh
cp .env.example .env
make build
docker compose config --quiet
```

Then run a dry-run local smoke and verify:

- dashboard loads
- `/api/status` online
- `/api/trades` reachable
- `/executor/health` reports `dry_run: true`
- `/forge/health` online
- JSONL files rotate under service-owned `logs/*` paths
- weekly report compiler produces one markdown report from fixture/sample logs

## Non-goals

- No live trading enablement.
- No exchange credential handling.
- No automatic strategy promotion.
- No Hermes/LLM hard dependency.
- No runtime log commits.
