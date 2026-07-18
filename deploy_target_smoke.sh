#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

printf '[1/6] dry-run env\n'
if [[ ! -f .env ]]; then
  cp .env.example .env
fi

grep -E '^(DRY_RUN|EXCHANGE_API|SA_CONFIDENCE)' .env || true

grep -q '^DRY_RUN=true$' .env || { echo 'STOP: DRY_RUN must be true'; exit 10; }
grep -q '^EXCHANGE_API_KEY=$' .env || { echo 'STOP: EXCHANGE_API_KEY must be empty'; exit 11; }
grep -q '^EXCHANGE_API_SECRET=$' .env || { echo 'STOP: EXCHANGE_API_SECRET must be empty'; exit 12; }
grep -q '^SA_CONFIDENCE_THRESHOLD=0.22$' .env || { echo 'STOP: SA_CONFIDENCE_THRESHOLD must be 0.22'; exit 13; }

printf '\n[2/6] docker + compose\n'
docker --version
docker compose version

printf '\n[3/6] port preflight\n'
if ss -ltnp | grep -E ':80|:18910|:18911|:18912'; then
  echo 'STOP: one or more required ports are already occupied'
  exit 20
fi

printf '\n[4/6] compose config\n'
docker compose config --quiet

printf '\n[5/6] build/start\n'
docker compose up -d --build

printf '\n[6/6] smoke\n'
docker compose ps
printf '\n--- nginx /api/status ---\n'
curl -fsS http://localhost/api/status | python3 -m json.tool
printf '\n--- direct predictor /api/status ---\n'
curl -fsS http://localhost:18910/api/status | python3 -m json.tool
printf '\n--- predictor logs ---\n'
docker compose logs predictor --tail 100
printf '\n--- signal_agent logs ---\n'
docker compose logs signal_agent --tail 100

printf '\nPASS: local smoke complete. Dashboard: http://localhost/ or http://$(hostname -I | awk '{print $1}')/\n'
