#!/usr/bin/env bash
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR/src"
exec "$DIR/.venv/bin/python" predictor_server.py "$@"
