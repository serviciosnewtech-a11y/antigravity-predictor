#!/usr/bin/env bash
set -u
cd "$(dirname "${BASH_SOURCE[0]}")"
echo "DATE: $(date -Is)"
echo "HOST: $(hostname)"
echo "IP: $(hostname -I 2>/dev/null || true)"
echo "ID: $(id)"
echo "GROUPS: $(groups 2>/dev/null || true)"
echo "--- docker ---"
docker --version 2>&1 || true
docker compose version 2>&1 || true
docker ps >/dev/null 2>&1 && echo "docker_access=direct" || sg docker -c 'docker ps >/dev/null 2>&1' && echo "docker_access=sg docker" || echo "docker_access=blocked"
echo "--- hermes proxy ---"
command -v hermes >/dev/null 2>&1 && hermes proxy status 2>&1 || echo "hermes command missing"
echo "--- ports ---"
ss -ltnp 2>/dev/null | grep -E ':(80|8645|18910|18911|18912)\b' || true
echo "--- backend host-port exposure check ---"
if ss -ltnp 2>/dev/null | grep -E ':(18910|18911|18912)\b'; then
  echo "backend_ports_exposed=yes"
else
  echo "backend_ports_exposed=no"
fi
echo "--- compose ps ---"
if docker ps >/dev/null 2>&1; then docker compose ps 2>&1 || true; else sg docker -c 'docker compose ps' 2>&1 || true; fi
echo "--- api ---"
curl -fsS http://localhost/api/status 2>&1 || true
echo
curl -fsS http://localhost/executor/health 2>&1 || true
echo
curl -fsS http://localhost/forge/health 2>&1 || true
echo
echo "--- direct backend ports should normally be unavailable from host after hardening ---"
curl -fsS --max-time 3 http://localhost:18910/api/status 2>&1 || true
echo
curl -fsS --max-time 3 http://localhost:18911/health 2>&1 || true
echo
curl -fsS --max-time 3 http://localhost:18912/health 2>&1 || true
echo "--- logs predictor ---"
if docker ps >/dev/null 2>&1; then docker compose logs predictor --tail 80 2>&1 || true; else sg docker -c 'docker compose logs predictor --tail 80' 2>&1 || true; fi
echo "--- logs signal_agent ---"
if docker ps >/dev/null 2>&1; then docker compose logs signal_agent --tail 80 2>&1 || true; else sg docker -c 'docker compose logs signal_agent --tail 80' 2>&1 || true; fi
