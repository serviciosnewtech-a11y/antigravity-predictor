"""
tests/test_bare_metal_agent_relay_wiring.py — regression test for two real
gaps found during a live bare-metal deploy of beta-1.10.3, fixed in
beta-1.10.4:

1. deploy/bare-metal/predictor.service had no EnvironmentFile= directive at
   all (unlike signal_agent.service, which always has). predictor_server.py
   has no dotenv loading of its own -- it reads os.environ.get() directly --
   so anything set in /opt/predictor/.env (HERMES_PROXY_URL, CHAT_BACKEND,
   ANTHROPIC_API_KEY, ...) was silently invisible to the process actually
   serving /api/chat, on every bare-metal install, regardless of which chat
   backend pattern was configured. This is a static/textual check (there's
   no systemd to actually run in this sandbox) but it directly guards
   against the exact regression that blocked a live deploy.

2. tools/agent_chat_relay.py (the local, no-API-key CLI-agent chat backend
   -- the documented default "ships with Hermes" pattern) was wired into
   run_monolith.sh but had no equivalent for the systemd bare-metal product
   at all -- no service unit, not even copied into $APP_DIR by install.sh.
   Guards that deploy/bare-metal/agent_relay.service exists and is wired
   correctly, and that install.sh copies tools/ and installs/enables/starts
   the new unit.

These are text-content assertions against the actual shipped files, not a
live systemd/network test -- see PRE_TEST_CHECKLIST.md's docker equivalent
for why that's an accepted, disclosed limitation of this sandbox. The actual
HTTP relay contract (tools/agent_chat_relay.py's /health, /api/chat,
/chat/completions routes, and the full predictor->llm_backend->relay chain)
was verified separately by running the real script with a stub
AGENT_RELAY_CMD and a real llm_backend.call_llm() call against it -- not
repeated here since that requires binding a real port, which is a poor fit
for a unit test that runs in CI alongside everything else.
"""
import os

_REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
_BARE_METAL_DIR = os.path.join(_REPO_ROOT, "deploy", "bare-metal")


def _read(rel_path: str) -> str:
    with open(os.path.join(_REPO_ROOT, rel_path)) as f:
        return f.read()


def test_predictor_service_loads_env_file():
    content = _read("deploy/bare-metal/predictor.service")
    assert "EnvironmentFile=-/opt/predictor/.env" in content, (
        "predictor.service must load /opt/predictor/.env -- without this, "
        "nothing in .env (chat backend config included) ever reaches the "
        "process serving /api/chat. This is the exact bug found in a live "
        "beta-1.10.3 deploy."
    )


def test_signal_agent_service_still_loads_env_file():
    # Regression guard the other direction: signal_agent.service already had
    # this right -- confirm the fix above didn't somehow get applied to the
    # wrong file instead of predictor.service.
    content = _read("deploy/bare-metal/signal_agent.service")
    assert "EnvironmentFile=-/opt/predictor/.env" in content


def test_agent_relay_service_exists_and_is_wired_correctly():
    path = os.path.join(_BARE_METAL_DIR, "agent_relay.service")
    assert os.path.exists(path), "deploy/bare-metal/agent_relay.service must exist"
    content = _read("deploy/bare-metal/agent_relay.service")
    assert "EnvironmentFile=-/opt/predictor/.env" in content
    assert "ExecStart=/opt/predictor/.venv/bin/python /opt/predictor/tools/agent_chat_relay.py" in content
    assert "WantedBy=multi-user.target" in content
    # Loopback only -- this relay must never be reachable off-host.
    assert "AGENT_RELAY_HOST=127.0.0.1" in content


def test_install_sh_copies_tools_directory():
    content = _read("deploy/bare-metal/install.sh")
    assert 'rsync -a --exclude=\'__pycache__\' --exclude=\'*.pyc\' \\\n    "$REPO_SRC/tools/"' in content, (
        "install.sh must copy tools/ -- agent_chat_relay.py lives there and "
        "was previously never shipped to $APP_DIR at all."
    )


def test_install_sh_installs_enables_and_starts_agent_relay():
    content = _read("deploy/bare-metal/install.sh")
    assert "agent_relay.service" in content
    assert "systemctl enable predictor macro_refresh.timer signal_agent agent_relay" in content
    assert "systemctl start agent_relay" in content


def test_install_sh_env_template_defaults_to_local_relay():
    content = _read("deploy/bare-metal/install.sh")
    assert "HERMES_PROXY_URL=http://127.0.0.1:8645" in content, (
        "the .env template should default HERMES_PROXY_URL at the local "
        "agent relay -- 'ships with Hermes by default' per the documented "
        "product intent, not silently unconfigured."
    )
    assert "SA_INFERENCE_BACKEND=disabled" in content


def test_install_sh_signal_agent_start_gate_uses_sa_inference_backend():
    # Regression guard: the old gate checked for a literal ANTHROPIC_API_KEY
    # value, which stopped being the right signal once SA_INFERENCE_BACKEND
    # became purely on/off (beta-1.10.3) -- a deploy using the local relay
    # or Ollama, with no Anthropic key at all, would have had signal_agent
    # silently skipped even with SA_INFERENCE_BACKEND=enabled.
    content = _read("deploy/bare-metal/install.sh")
    assert 'grep -E \'^SA_INFERENCE_BACKEND=\'' in content
    assert 'grep -q "ANTHROPIC_API_KEY=."' not in content
