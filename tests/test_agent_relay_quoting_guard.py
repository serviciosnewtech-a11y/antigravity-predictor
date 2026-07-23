"""
tests/test_agent_relay_quoting_guard.py — regression test for the {prompt}
quoting pitfall found during a live bare-metal deploy 2026-07-23 (see
deploy/bare-metal/LIVE_DEPLOY_NOTES.md, section 6).

tools/agent_chat_relay.py substitutes {prompt} in AGENT_RELAY_CMD via
shlex.quote(), which is only a safe quoting boundary when {prompt} is the
OUTERMOST quoting context in the template. A template that wraps {prompt}
in its own quotes (e.g. '/bin/echo "reply: {prompt}"') breaks that guarantee
the moment the real prompt contains an embedded quote character -- which a
real system prompt will, but a trivial health-check probe string ("ping")
won't, so the failure is invisible until a real chat request happens. A
startup warning was added to catch the common case (a quote character
touching either side of the placeholder) -- these tests exercise it via
subprocess, since tools/agent_chat_relay.py is designed to run standalone
(python3 tools/agent_chat_relay.py), not be imported as a package, and its
{prompt}-handling logic runs at module-import time based on the AGENT_RELAY_CMD
env var read at that moment.
"""
import os
import subprocess
import sys

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_RELAY_SCRIPT = os.path.join(_REPO_ROOT, "tools", "agent_chat_relay.py")


def _import_relay_with_env(agent_relay_cmd: str) -> str:
    """Runs a one-shot `import agent_chat_relay` with AGENT_RELAY_CMD set,
    in a fresh subprocess (so module-level code re-runs cleanly each time),
    and returns combined stdout+stderr. The import alone is enough to
    trigger the startup checks -- ThreadingHTTPServer binding only happens
    in main(), which we never call."""
    env = dict(os.environ)
    env["AGENT_RELAY_CMD"] = agent_relay_cmd
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'tools'); import agent_chat_relay"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout + result.stderr


def test_warns_when_prompt_wrapped_in_double_quotes():
    out = _import_relay_with_env('/bin/echo "[reply] {prompt}"')
    assert "wraps {prompt} in its own quote characters" in out


def test_warns_when_prompt_wrapped_in_single_quotes():
    out = _import_relay_with_env("/bin/echo '[reply] {prompt}'")
    assert "wraps {prompt} in its own quote characters" in out


def test_no_warning_for_correctly_unquoted_placeholder():
    out = _import_relay_with_env("/bin/echo [reply] {prompt}")
    assert "wraps {prompt} in its own quote characters" not in out


def test_no_warning_for_default_template():
    # Don't set AGENT_RELAY_CMD at all -- exercises the actual shipped default.
    env = dict(os.environ)
    env.pop("AGENT_RELAY_CMD", None)
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.path.insert(0, 'tools'); import agent_chat_relay"],
        cwd=_REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=15,
    )
    out = result.stdout + result.stderr
    assert "wraps {prompt} in its own quote characters" not in out
