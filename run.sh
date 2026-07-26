#!/usr/bin/env bash
# Bare-metal launcher. On first run, bootstraps .venv from requirements.txt
# (venvs aren't portable, so the tarball ships without one). Subsequent
# runs skip the bootstrap and just exec the server.
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$DIR/.venv"
if [ ! -x "$VENV/bin/python" ]; then
  echo "[run.sh] .venv missing — bootstrapping (first run, ~1-2 min)..."
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install --upgrade pip >/dev/null
  "$VENV/bin/pip" install -r "$DIR/requirements.txt"
  echo "[run.sh] .venv ready."
fi
cd "$DIR/src"
exec "$VENV/bin/python" predictor_server.py "$@"
