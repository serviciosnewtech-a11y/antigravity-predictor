"""
tests/test_agent_relay_warmup.py — regression test for a real live latency
bug found 2026-07-23: the first invocation of a real CLI agent after a
fresh process/session start can carry real one-time cold-start cost (model
load, provider handshake, session init) -- one measured live case took
~15s on the first call, comfortably under a second on every call after.

Every prior test in this repo used instant stub commands (echo), so this
never surfaced until a real agent was wired in. The failure mode this
produces: whatever per-request timeout is configured (predictor's outbound
call, or the relay's own AGENT_RELAY_TIMEOUT_S) times out on the very first
real user message, well before the agent's cold-start finishes, and
/api/chat reports agent_unavailable even though the backend is genuinely
fine -- it's just slow exactly once.

The wrong fix is raising the per-request timeout to cover the cold-start
case, since that also makes every real user wait through it and doesn't
help after every service restart. The actual fix: tools/agent_chat_relay.py
now runs a one-time warm-up invocation at process startup, before the
server starts accepting connections, absorbing the cold-start cost once so
real requests never pay it.

These tests actually start the relay as a real subprocess bound to a real
port (matching how it actually runs in production) rather than importing
it as a module, since the warm-up behavior is inherently about process
startup timing.
"""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_RELAY_SCRIPT = os.path.join(_REPO_ROOT, "tools", "agent_chat_relay.py")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_health(port: int, timeout_s: float) -> dict:
    deadline = time.time() + timeout_s
    last_err = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
                return json.loads(resp.read())
        except Exception as e:
            last_err = e
            time.sleep(0.1)
    raise TimeoutError(f"relay never became reachable on port {port}: {last_err}")


def _start_relay(port: int, extra_env: dict, slow_stub_path: str = None):
    env = dict(os.environ)
    env["AGENT_RELAY_PORT"] = str(port)
    env["PYTHONUNBUFFERED"] = "1"
    env.update(extra_env)
    proc = subprocess.Popen(
        [sys.executable, _RELAY_SCRIPT],
        cwd=_REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    return proc


def test_warmup_absorbs_cold_start_so_first_real_request_is_fast(tmp_path):
    """The core regression guard: a stub simulating a slow first call (like
    a real agent's cold start) should be fully absorbed by the startup
    warm-up -- by the time the server is reachable, /health should already
    report a cached, successful result, not force a caller to wait through
    the slow path themselves."""
    slow_stub = tmp_path / "slow_stub.sh"
    slow_stub.write_text("#!/bin/bash\nsleep 2\necho \"[reply] $1\"\n")
    slow_stub.chmod(0o755)

    port = _free_port()
    proc = _start_relay(port, {
        "AGENT_RELAY_CMD": f"{slow_stub} {{prompt}}",
        "AGENT_RELAY_WARMUP_TIMEOUT_S": "30",
    })
    try:
        # Generous wait for the relay to become reachable -- this window
        # covers the ~2s warm-up call happening internally before the
        # server starts accepting connections at all.
        health = _wait_for_health(port, timeout_s=15)

        assert health["agent_binary_exists"] is True
        assert health["live_ok"] is True
        # The key assertion: warm-up already ran and populated the cache
        # before we could even reach /health, so this is a cached result,
        # not a fresh probe we had to wait through ourselves.
        assert health["cached"] is True

        # And a real call, right after startup, should be fast -- the
        # slow_stub's 2s sleep only ever happened once, during warm-up.
        start = time.monotonic()
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/health", timeout=5
        ) as resp:
            json.loads(resp.read())
        elapsed = time.monotonic() - start
        assert elapsed < 1.0, f"expected a fast cached health response, took {elapsed:.2f}s"
    finally:
        proc.terminate()
        proc.wait(timeout=5)


def test_skip_warmup_env_var_disables_it(tmp_path):
    slow_stub = tmp_path / "slow_stub.sh"
    slow_stub.write_text("#!/bin/bash\nsleep 0.3\necho \"[reply] $1\"\n")
    slow_stub.chmod(0o755)

    port = _free_port()
    proc = _start_relay(port, {
        "AGENT_RELAY_CMD": f"{slow_stub} {{prompt}}",
        "AGENT_RELAY_SKIP_WARMUP": "true",
    })
    try:
        health = _wait_for_health(port, timeout_s=10)
        # No warm-up ran, so this /health call had to do its own live probe.
        assert health["cached"] is False
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except Exception:
            out = ""
        proc.wait(timeout=5)
    assert "Startup warm-up skipped" in out


def test_warmup_failure_does_not_block_server_from_starting(tmp_path):
    """A warm-up call that fails (binary that always errors) must not
    prevent the relay from starting -- it should log a warning and still
    bind the port, matching this codebase's established graceful-
    degradation philosophy (report real status via /health rather than
    refusing to start)."""
    failing_stub = tmp_path / "failing_stub.sh"
    failing_stub.write_text("#!/bin/bash\necho 'fatal: nope' >&2\nexit 1\n")
    failing_stub.chmod(0o755)

    port = _free_port()
    proc = _start_relay(port, {
        "AGENT_RELAY_CMD": f"{failing_stub} {{prompt}}",
        "AGENT_RELAY_WARMUP_TIMEOUT_S": "5",
    })
    try:
        health = _wait_for_health(port, timeout_s=10)
        assert health["agent_binary_exists"] is True
        assert health["live_ok"] is False
    finally:
        proc.terminate()
        proc.wait(timeout=5)
