#!/usr/bin/env python3
"""
tools/agent_chat_relay.py — local, agent-agnostic CLI-backend relay for chat surfaces.

Bridges predictor_server.py's chat surfaces (which only ever speak
OpenAI-style POST /chat/completions via HERMES_PROXY_URL — see
_call_llm_backend() in src/predictor_server.py) to ANY locally-installed
CLI-based agent — not tied to a specific tool, binary, or persona name.
Use this when the inference backend is a local CLI rather than a hosted
HTTP API — e.g. no internet access for a remote provider, or you want a
specific local agent's own persona/session handling driving replies.

Agent-agnostic by design: AGENT_RELAY_CMD is a full shell command template
with a single {prompt} placeholder. Whatever you put there runs, with the
flattened conversation text substituted in (safely shell-quoted) — it can
be `hermes --profile metis chat -q {prompt}`, a different hermes profile,
a completely different CLI tool, or your own script. This relay does not
know or care what's on the other end; it just runs the command and reads
stdout back as the reply. The previous version of this file (tools/
metis_chat_relay.py) was Hermes-specific; this replaces it with the same
mechanism generalized to any agent.

This is a zero-extra-dependency stdlib HTTP server on purpose — it needs
to run standalone, independent of the main FastAPI app's venv/imports.

Routes:
    POST /api/chat              — predictor's native chat payload
                                   ({message, symbol, language, history})
                                   -> {reply, source, signal, timestamp}
    POST /chat/completions       -- OpenAI-style {messages: [...]}
    POST /v1/chat/completions    -- (both accepted; predictor calls
                                   f"{HERMES_PROXY_URL}/chat/completions",
                                   so whichever prefix your HERMES_PROXY_URL
                                   ends up with, both routes work)
    GET  /health                 — relay + configured-agent sanity check
    anything else                -- 404

Configuration (env vars, all optional):
    AGENT_RELAY_CMD           Full shell command template. Must contain the
                               literal "{prompt}" placeholder exactly once —
                               it is replaced with the flattened, shell-
                               quoted conversation text before execution.
                               Default: 'hermes --profile metis chat -q {prompt}'
                               (preserves this relay's original Hermes/Metis
                               behavior with zero config changes needed).
                               Examples:
                                 AGENT_RELAY_CMD='hermes --profile ops chat -q {prompt}'
                                 AGENT_RELAY_CMD='mycli --agent trader ask {prompt}'
                                 AGENT_RELAY_CMD='python3 tools/my_agent.py {prompt}'
    AGENT_RELAY_PORT           Port to bind. Default: 8645 (matches the
                               HERMES_PROXY_URL placeholder already used in
                               .env.example, so this "just works" with the
                               zero-config default there).
    AGENT_RELAY_HOST           Bind host. Default: 127.0.0.1 (loopback only
                               — this relay is meant to be reached by the
                               predictor process on the same machine, not
                               exposed to the network).
    AGENT_RELAY_TIMEOUT_S      Subprocess timeout for one agent invocation.
                               Default: 120.
    AGENT_RELAY_HISTORY_TURNS  How many trailing messages to fold into the
                               flattened prompt. Default: 8.

Run standalone:
    python3 tools/agent_chat_relay.py
Then point the predictor at it (in .env, or exported before start.sh):
    HERMES_PROXY_URL=http://127.0.0.1:8645
    HERMES_INFERENCE_MODEL=local-agent-cli   # cosmetic only, relay ignores this
Or let run_monolith.sh manage its lifecycle automatically — see
ENABLE_AGENT_RELAY in .env.example.
"""
from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_DEFAULT_CMD = "hermes --profile metis chat -q {prompt}"

AGENT_CMD = os.environ.get("AGENT_RELAY_CMD", _DEFAULT_CMD)
RELAY_PORT = int(os.environ.get("AGENT_RELAY_PORT", "8645"))
RELAY_HOST = os.environ.get("AGENT_RELAY_HOST", "127.0.0.1")
COMMAND_TIMEOUT_S = float(os.environ.get("AGENT_RELAY_TIMEOUT_S", "120"))
HISTORY_TURNS = int(os.environ.get("AGENT_RELAY_HISTORY_TURNS", "8"))

if "{prompt}" not in AGENT_CMD:
    print(
        f"[agent_relay] WARNING: AGENT_RELAY_CMD={AGENT_CMD!r} has no {{prompt}} "
        f"placeholder — the conversation text will never reach the agent. "
        f"Falling back to the default command."
    )
    AGENT_CMD = _DEFAULT_CMD


def _agent_binary_name() -> str:
    """Best-effort extraction of the first token (the binary) from the
    command template, for the /health sanity check only — never used to
    actually run anything."""
    try:
        placeholder_free = AGENT_CMD.replace("{prompt}", "x")
        parts = shlex.split(placeholder_free)
        return parts[0] if parts else AGENT_CMD
    except Exception:
        return AGENT_CMD


def _invoke_agent(prompt: str) -> str:
    """Run AGENT_RELAY_CMD with {prompt} substituted (shell-quoted) and
    capture stdout as the reply. Falls back to stderr if stdout is empty,
    so an agent-side warning/error still surfaces as a reply instead of
    silently returning blank text."""
    cmd = AGENT_CMD.replace("{prompt}", shlex.quote(prompt))
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"[agent_relay] agent call timed out after {COMMAND_TIMEOUT_S}s"
    except Exception as e:
        return f"[agent_relay] failed to invoke agent: {e}"
    out = (proc.stdout or "").strip()
    if not out:
        out = (proc.stderr or "").strip() or "[agent_relay] agent returned no output"
    return out


def _flatten_messages(messages: list) -> str:
    tail = messages[-HISTORY_TURNS:] if messages else []
    lines = [f"[{m.get('role', 'user')}] {m.get('content', '')}" for m in tail]
    return "\n".join(lines)


def _infer_signal(text: str) -> str:
    upper = text.upper()
    if "EXIT" in upper:
        return "EXIT"
    has_buy = "BUY" in upper
    has_sell = "SELL" in upper
    if has_buy and not has_sell:
        return "BUY"
    if has_sell and not has_buy:
        return "SELL"
    return "NEUTRAL"


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter default logging
        print(f"[agent_relay] {self.address_string()} - {fmt % args}")

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            binary = _agent_binary_name()
            self._send_json(200, {
                "status": "online",
                "agent_cmd": AGENT_CMD,
                "agent_binary": binary,
                "agent_binary_exists": shutil.which(binary) is not None or os.path.exists(binary),
            })
        else:
            self._send_json(404, {"error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except Exception:
            self._send_json(400, {"error": "invalid_json"})
            return

        if self.path == "/api/chat":
            message = body.get("message", "")
            symbol = body.get("symbol", "")
            history = body.get("history", [])
            prompt_lines = []
            if symbol:
                prompt_lines.append(f"[context] symbol={symbol}")
            prompt_lines.append(_flatten_messages(history))
            prompt_lines.append(f"[user] {message}")
            prompt = "\n".join(p for p in prompt_lines if p)

            reply = _invoke_agent(prompt)
            self._send_json(200, {
                "reply": reply,
                "source": "agent-relay",
                "signal": _infer_signal(reply),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return

        if self.path in ("/chat/completions", "/v1/chat/completions"):
            messages = body.get("messages", [])
            prompt = _flatten_messages(messages)
            reply = _invoke_agent(prompt)
            model = body.get("model") or "local-agent-cli"
            self._send_json(200, {
                "id": "chatcmpl-agent-relay",
                "object": "chat.completion",
                "created": int(time.time()),
                "model": model,
                "choices": [{
                    "index": 0,
                    "message": {"role": "assistant", "content": reply},
                    "finish_reason": "stop",
                }],
                "usage": {
                    "prompt_tokens": max(1, len(prompt) // 4),
                    "completion_tokens": max(1, len(reply) // 4),
                    "total_tokens": max(1, (len(prompt) + len(reply)) // 4),
                },
            })
            return

        self._send_json(404, {"error": "not_found", "path": self.path})


def main() -> int:
    binary = _agent_binary_name()
    print(
        f"[agent_relay] Starting on {RELAY_HOST}:{RELAY_PORT} — "
        f"cmd={AGENT_CMD!r} timeout={COMMAND_TIMEOUT_S}s"
    )
    if not (shutil.which(binary) or os.path.exists(binary)):
        print(
            f"[agent_relay] WARNING: {binary!r} (first token of AGENT_RELAY_CMD) not found "
            f"on PATH or as a file. Requests will fail until AGENT_RELAY_CMD points at a "
            f"real, runnable agent."
        )
    server = ThreadingHTTPServer((RELAY_HOST, RELAY_PORT), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
