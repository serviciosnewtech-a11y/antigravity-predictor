#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

need() { command -v "$1" >/dev/null 2>&1 || { echo "BLOCKED: missing command: $1"; exit 10; }; }
load_env() { set -a; source .env; set +a; }

ensure_internal_token() {
  python3 - <<'PY'
from pathlib import Path
import secrets

path = Path('.env')
lines = path.read_text().splitlines()
found = False
out = []
for line in lines:
    if line.startswith('INTERNAL_API_TOKEN='):
        found = True
        key, value = line.split('=', 1)
        if not value.strip():
            line = f'{key}={secrets.token_hex(32)}'
    out.append(line)
if not found:
    out.append(f'INTERNAL_API_TOKEN={secrets.token_hex(32)}')
path.write_text('\n'.join(out) + '\n')
PY
}

[ -f .env ] || cp .env.example .env
need python3
ensure_internal_token
load_env

[ "${DRY_RUN:-}" = "true" ] || { echo "BLOCKED: DRY_RUN must be true for demo"; exit 11; }
[ -z "${EXCHANGE_API_KEY:-}" ] || { echo "BLOCKED: EXCHANGE_API_KEY must be empty for demo"; exit 12; }
[ -z "${EXCHANGE_API_SECRET:-}" ] || { echo "BLOCKED: EXCHANGE_API_SECRET must be empty for demo"; exit 13; }

ensure_proxy() {
  if [ "${SA_INFERENCE_BACKEND:-disabled}" != "openai_compatible" ] && [ "${SA_INFERENCE_BACKEND:-}" != "hermes" ] && [ "${SA_INFERENCE_BACKEND:-}" != "hermes_proxy" ]; then
    echo "LLM enrichment disabled or external; skipping Hermes proxy startup."
    return 0
  fi
  if ! command -v hermes >/dev/null 2>&1; then
    echo "BLOCKED: hermes command missing; cannot start Hermes proxy"
    exit 14
  fi
  hermes proxy status || true
  if ss -ltn 2>/dev/null | grep -q ':8645 '; then
    return 0
  fi
  nohup hermes proxy start --provider nous --host 127.0.0.1 --port 8645 > logs/hermes-proxy.log 2>&1 &
  for _ in $(seq 1 20); do
    ss -ltn 2>/dev/null | grep -q ':8645 ' && return 0
    sleep 1
  done
  echo "BLOCKED: Hermes proxy did not start on 127.0.0.1:8645"
  tail -80 logs/hermes-proxy.log 2>/dev/null || true
  exit 15
}

docker_cmd() {
  if docker ps >/dev/null 2>&1; then
    docker "$@"
  elif sg docker -c 'docker ps >/dev/null 2>&1'; then
    sg docker -c "docker $*"
  else
    echo "BLOCKED: docker socket inaccessible; user=$(id)"
    exit 16
  fi
}
compose_cmd() {
  if docker ps >/dev/null 2>&1; then
    docker compose "$@"
  elif sg docker -c 'docker ps >/dev/null 2>&1'; then
    sg docker -c "docker compose $*"
  else
    echo "BLOCKED: docker compose inaccessible; user=$(id)"
    exit 17
  fi
}

check_ports() {
  local out
  local dashboard_port="${DASHBOARD_PORT:-80}"
  local dashboard_url="http://localhost:${dashboard_port}"
  out=$(ss -ltnp 2>/dev/null | grep -E ":${dashboard_port}\\b" || true)
  [ -z "$out" ] && return 0
  if curl -fsS --max-time 5 "${dashboard_url}/api/status" >/dev/null 2>&1 \
    && curl -fsS --max-time 5 "${dashboard_url}/executor/health" >/dev/null 2>&1 \
    && curl -fsS --max-time 5 "${dashboard_url}/forge/health" >/dev/null 2>&1; then
    echo "Existing Antigravity stack is responding; treating required ports as redeploy-safe."
    return 0
  fi
  if echo "$out" | grep -E 'docker-proxy|containerd|dockerd|antigravity|predictor' >/dev/null; then
    echo "Existing app/docker port listeners detected; treating as redeploy-safe:"
    echo "$out"
    return 0
  fi
  echo "BLOCKED: unrelated required port owner detected"
  echo "$out"
  exit 18
}

warn_backend_ports() {
  local out
  out=$(ss -ltnp 2>/dev/null | grep -E ':(18910|18911|18912)\b' || true)
  [ -z "$out" ] && return 0
  echo "WARNING: backend host ports are listening before redeploy; next compose up should remove Antigravity-owned bindings:"
  echo "$out"
}

wait_for_url() {
  local url="$1"
  local name="$2"
  for _ in $(seq 1 45); do
    if curl -fsS --max-time 5 "$url" >/dev/null 2>&1; then
      echo "READY: $name"
      return 0
    fi
    sleep 2
  done
  echo "BLOCKED: $name did not become ready: $url"
  compose_cmd ps || true
  compose_cmd logs dashboard --tail 80 || true
  compose_cmd logs predictor --tail 120 || true
  exit 19
}

mkdir -p logs
need curl
need ss
need python3
ensure_proxy
check_ports
warn_backend_ports
compose_cmd config --quiet
compose_cmd up -d --build
compose_cmd ps
dashboard_port="${DASHBOARD_PORT:-80}"
dashboard_url="http://localhost:${dashboard_port}"
wait_for_url "${dashboard_url}/api/status" dashboard-proxy
wait_for_url "${dashboard_url}/executor/health" executor-proxy
wait_for_url "${dashboard_url}/forge/health" forge-proxy
curl -fsS "${dashboard_url}/api/status" | python3 -m json.tool
curl -fsS "${dashboard_url}/executor/health" | python3 -m json.tool || curl -fsS "${dashboard_url}/executor/health"
curl -fsS "${dashboard_url}/forge/health" | python3 -m json.tool || curl -fsS "${dashboard_url}/forge/health"
compose_cmd logs predictor --tail 80 || true
compose_cmd logs signal_agent --tail 80 || true
echo "Dashboard: http://$(hostname -I | awk '{print $1}'):${dashboard_port}/"
