#!/usr/bin/env bash
# tools/run_tests.sh — run the test suite isolated from ambient shell
# Python state.
#
# Background: `pytest tests/` can fail with import errors like
# `ModuleNotFoundError: pydantic_core._pydantic_core` even though the
# project's own .venv has a perfectly good install, if the shell has a
# PYTHONPATH (or PYTHONHOME) set that points at a DIFFERENT Python
# installation's site-packages (e.g. a system Python 3.11 next to this
# project's .venv Python 3.12) — that external path gets prepended ahead
# of the venv's own packages, so an incompatible compiled extension gets
# picked up instead. This is an environment issue, not a project bug, but
# it's avoidable: explicitly unset the polluting vars and invoke the
# venv's own python directly instead of relying on `pytest` from PATH.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")/.."

if [[ ! -x ".venv/bin/python" ]]; then
    echo "ERROR: .venv/bin/python not found. Create it first: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt"
    exit 1
fi

unset PYTHONPATH
unset PYTHONHOME
export PYTHONNOUSERSITE=1

echo "[run_tests] Using: $(.venv/bin/python --version) at .venv/bin/python (PYTHONPATH/PYTHONHOME unset for isolation)"
exec .venv/bin/python -m pytest tests/ -v "$@"
