#!/usr/bin/env python3
"""
tools/metis_chat_relay.py — local CLI-backend relay for chat surfaces.

Bridges predictor_server.py's chat surfaces (which only ever speak
OpenAI-style POST /chat/completions via HERMES_PROXY_URL — see
_call_llm_backend() in src/predictor_server.py) to a locally-installed
`hermes` CLI persona (default profile: "metis", matching the Crypto
Operator Agent / CRYPTO_OPERATOR_SOUL.md persona). Use this when the
inference backend is a local CLI tool rather than a hosted HTTP API —
e.g. no internet access for a remote provider, or you specifically want
the Hermes CLI's own persona/session handling driving replies.

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
    GET  /health                 — relay + hermes-binary sanity check
    anything else                -- 404

Configuration (env vars, all optional):
    METIS_HERMES_BIN         Path to the hermes executable.
                              Default: auto-detected via `which hermes`,
                              falling back to "hermes" on PATH.
    METIS_HERMES_PROFILE     Hermes CLI profile to invoke. Default: "metis".
    METIS_RELAY_PORT         Port to bind. Default: 8645 (matches the
                              HERMES_PROXY_URL placeholder already used in
                              .env.example, so this "just works" with the
                              zero-config default there).
    METIS_RELAY_HOST         Bind host. Default: 127.0.0.1 (loopback only
                              — this relay is meant to be reached by the
                              predictor process on the same machine, not
                              exposed to the network).
    METIS_COMMAND_TIMEOUT_S  Subprocess timeout for one hermes invocation.
                              Default: 120.
    METIS_HISTORY_TURNS      How many trailing messages to fold into the
                              flattened prompt. Default: 8.

Run standalone:
    python3 tools/metis_chat_relay.py
Then point the predictor at it (in .env, or exported before start.sh):
    HERMES_PROXY_URL=http://127.0.0.1:8645
    HERMES_INFERENCE_MODEL=metis-cli   # cosmetic only, relay ignores this
Or let run_monolith.sh manage its lifecycle automatically — see
ENABLE_METIS_RELAY in .env.example.
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

HERMES_BIN = os.environ.get("METIS_HERMES_BIN") or shutil.which("hermes") or "hermes"
HERMES_PROFILE = os.environ.get("METIS_HERMES_PROFILE", "metis")
RELAY_PORT = int(os.environ.get("METIS_RELAY_PORT", "8645"))
RELAY_HOST = os.environ.get("METIS_RELAY_HOST", "127.0.0.1")
COMMAND_TIMEOUT_S = float(os.environ.get("METIS_COMMAND_TIMEOUT_S", "120"))
HISTORY_TURNS = int(os.environ.get("METIS_HISTORY_TURNS", "8"))


def _invoke_metis(prompt: str) -> str:
    """Shell out to `hermes --profile <profile> chat -q <prompt>`.
    shlex.quote keeps the prompt safe as a single shell argument regardless
    of its contents (quotes, newlines, etc.). Falls back to stderr if
    stdout is empty, so a Hermes-side warning/error still surfaces as a
    reply instead of silently returning blank text."""
    cmd = f"{shlex.quote(HERMES_BIN)} --profile {shlex.quote(HERMES_PROFILE)} chat -q {shlex.quote(prompt)}"
    try:
        proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return f"[metis_relay] hermes call timed out after {COMMAND_TIMEOUT_S}s"
    except Exception as e:
        return f"[metis_relay] failed to invoke hermes: {e}"
    out = (proc.stdout or "").strip()
    if not out:
        out = (proc.stderr or "").strip() or "[metis_relay] hermes returned no output"
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
        print(f"[metis_relay] {self.address_string()} - {fmt % args}")

    def _send_json(self, status: int, payload: dict):
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._send_json(200, {
                "status": "online",
                "hermes_bin": HERMES_BIN,
                "hermes_bin_exists": shutil.which(HERMES_BIN) is not None or os.path.exists(HERMES_BIN),
                "profile": HERMES_PROFILE,
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

            reply = _invoke_metis(prompt)
            self._send_json(200, {
                "reply": reply,
                "source": "metis-cli",
                "signal": _infer_signal(reply),
                "timestamp": datetime.now(timezone.utc).isoformat(),
            })
            return

        if self.path in ("/chat/completions", "/v1/chat/completions"):
            messages = body.get("messages", [])
            prompt = _flatten_messages(messages)
            reply = _invoke_metis(prompt)
            model = body.get("model") or "metis-cli"
            self._send_json(200, {
                "id": "chatcmpl-metis",
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
    print(
        f"[metis_relay] Starting on {RELAY_HOST}:{RELAY_PORT} — "
        f"hermes_bin={HERMES_BIN!r} profile={HERMES_PROFILE!r} "
        f"timeout={COMMAND_TIMEOUT_S}s"
    )
    if not (shutil.which(HERMES_BIN) or os.path.exists(HERMES_BIN)):
        print(
            f"[metis_relay] WARNING: {HERMES_BIN!r} not found on PATH or as a file. "
            f"Requests will fail until METIS_HERMES_BIN points at a real hermes binary."
        )
    server = ThreadingHTTPServer((RELAY_HOST, RELAY_PORT), _Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
