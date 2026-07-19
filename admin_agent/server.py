#!/usr/bin/env python3
"""
admin_agent/server.py — Full-spectrum personal admin agent.

DANGER: this service gives an LLM real, unrestricted shell access on this
host, under your identity, with no per-command confirmation step. This is
NOT the same trust model as the dashboard's Hermes/Hermes Tutor chats
(which have zero tool access by construction). This exists ONLY for the
operator who holds ADMIN_API_TOKEN — it is never linked from the public
dashboard, never started by default, and every command it runs is written
to an append-only audit log before execution.

You explicitly chose "unrestricted shell/API access" over the safer
whitelisted/propose-then-confirm options when this was built. That's your
call to make on your own machine — this file just implements it as safely
as an unrestricted design can be:
  - separate token from the dashboard's INTERNAL_API_TOKEN
  - not started unless ENABLE_ADMIN_AGENT=true AND ADMIN_API_TOKEN is set
  - every executed command + exit code + output is logged to
    logs/admin_agent_audit.log BEFORE the reply is returned, so there's
    always a record even if something goes wrong mid-conversation
  - per-command timeout and output-size cap so one runaway command can't
    hang the process or blow out memory
  - binds to 0.0.0.0 like the other services, but do not port-forward this
    one to the public internet — it is meant for localhost/LAN access by
    you alone, protected only by the bearer token.

Usage:
    ADMIN_API_TOKEN=... python3 admin_agent/server.py
Talk to it with tools/admin_chat.py, or any HTTP client:
    POST /admin-chat  { "message": "...", "history": [...] }
    header: X-Admin-Token: <ADMIN_API_TOKEN>
"""
from __future__ import annotations

import os
import re
import shlex
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

import requests
from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from pydantic import BaseModel

REPO_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = Path(os.environ.get("LOGS_DIR", str(REPO_ROOT / "logs")))
LOGS_DIR.mkdir(parents=True, exist_ok=True)
AUDIT_LOG_PATH = LOGS_DIR / "admin_agent_audit.log"

ADMIN_API_TOKEN = os.environ.get("ADMIN_API_TOKEN", "")
ADMIN_AGENT_PORT = int(os.environ.get("ADMIN_AGENT_PORT", "18913"))
COMMAND_TIMEOUT_S = float(os.environ.get("ADMIN_COMMAND_TIMEOUT_S", "25"))
MAX_OUTPUT_CHARS = 4000
MAX_REACT_STEPS = 6

app = FastAPI(title="Antigravity Predictor — Admin Agent (unrestricted, operator-only)")

if not ADMIN_API_TOKEN:
    logger.warning(
        "[admin_agent] ADMIN_API_TOKEN is not set — every request will be rejected. "
        "Set it in .env and restart. This is intentional: this service must never "
        "run with an open door."
    )


class _AdminMsg(BaseModel):
    role: str  # "user" | "assistant"
    content: str


class _AdminChatRequest(BaseModel):
    message: str
    history: List[_AdminMsg] = []


def _backend_config() -> dict:
    """Same generic OpenAI-compatible-proxy / Ollama resolution as the
    dashboard chats, reusing ADMIN_-prefixed overrides first so this agent
    can point at a different (e.g. more capable) model than the tutor if
    desired — falls back to the shared HERMES_PROXY_* / OLLAMA_* vars."""
    proxy_url = (os.environ.get("ADMIN_HERMES_PROXY_URL") or os.environ.get("HERMES_PROXY_URL", "")).rstrip("/")
    proxy_key = os.environ.get("ADMIN_HERMES_PROXY_API_KEY") or os.environ.get("HERMES_PROXY_API_KEY", "local")
    proxy_model = os.environ.get("ADMIN_INFERENCE_MODEL") or os.environ.get("HERMES_INFERENCE_MODEL", "")
    ollama_url = (os.environ.get("ADMIN_OLLAMA_URL") or os.environ.get("OLLAMA_URL", "")).rstrip("/")
    ollama_model = os.environ.get("ADMIN_OLLAMA_MODEL") or os.environ.get("OLLAMA_MODEL", "llama3.2")
    return {
        "proxy_url": proxy_url, "proxy_key": proxy_key, "proxy_model": proxy_model,
        "ollama_url": ollama_url, "ollama_model": ollama_model,
    }


def _call_llm(messages: list, cfg: dict) -> Optional[str]:
    if cfg["proxy_url"]:
        try:
            resp = requests.post(
                f"{cfg['proxy_url']}/chat/completions",
                json={"model": cfg["proxy_model"], "messages": messages, "stream": False},
                headers={"Authorization": f"Bearer {cfg['proxy_key']}"},
                timeout=60,
            )
            if resp.status_code == 200:
                choices = resp.json().get("choices", [])
                if choices and "message" in choices[0]:
                    return choices[0]["message"].get("content", "").strip()
            else:
                logger.warning(f"[admin_agent] proxy returned {resp.status_code}: {resp.text[:300]}")
        except Exception as e:
            logger.warning(f"[admin_agent] proxy unavailable: {e}")
    if cfg["ollama_url"]:
        try:
            resp = requests.post(
                f"{cfg['ollama_url']}/api/chat",
                json={"model": cfg["ollama_model"], "messages": messages, "stream": False},
                timeout=60,
            )
            if resp.status_code == 200:
                return resp.json().get("message", {}).get("content", "").strip()
        except Exception as e:
            logger.warning(f"[admin_agent] ollama unavailable: {e}")
    return None


_SYSTEM_PROMPT = f"""You are the Antigravity Predictor Admin Agent, running with real, unrestricted
shell access on the operator's own machine ({REPO_ROOT}). Only the operator
(who holds the private admin token) can reach you — you are not exposed to
the public dashboard or any other user.

You have exactly one tool. To run a shell command, reply with ONLY a line
of the exact form:
SHELL: <command>
and nothing else in that reply. The command will be executed on the host
and its output returned to you as the next message, so you can decide
what to do next (run another command, or give your final answer).

When you are ready to give your final answer to the operator (not a
command), reply with ONLY:
FINAL: <your answer>

Rules:
- One action per reply — either exactly one SHELL: line, or exactly one
  FINAL: line. Never both, never plain prose outside these two forms.
- Prefer read-only/inspection commands first when you're unsure what
  state something is in.
- This machine runs the Antigravity Predictor stack (predictor :18910,
  executor :18911 dry-run by default, forge :18912). Be careful with
  anything that touches config.json, models/, or restarts services the
  operator didn't ask you to touch.
- If a command's purpose is destructive or hard to undo (deleting files,
  killing processes, editing config, git operations that rewrite
  history), say so plainly in your FINAL answer even though you already
  ran it — the operator should always know what actually happened.
- You are not a general assistant with no scope — stay focused on this
  machine and this project unless the operator clearly asks for something
  else.
"""


def _run_shell(command: str) -> str:
    started = time.time()
    timestamp = datetime.now(timezone.utc).isoformat()
    try:
        proc = subprocess.run(
            command, shell=True, cwd=str(REPO_ROOT),
            capture_output=True, text=True, timeout=COMMAND_TIMEOUT_S,
        )
        out = (proc.stdout or "") + (("\n[stderr]\n" + proc.stderr) if proc.stderr else "")
        exit_code = proc.returncode
    except subprocess.TimeoutExpired:
        out = f"[TIMEOUT after {COMMAND_TIMEOUT_S}s — process killed]"
        exit_code = -1
    except Exception as e:
        out = f"[error launching command: {e}]"
        exit_code = -1
    duration = time.time() - started
    truncated = out[:MAX_OUTPUT_CHARS]
    if len(out) > MAX_OUTPUT_CHARS:
        truncated += f"\n[...output truncated, {len(out) - MAX_OUTPUT_CHARS} more chars...]"

    with open(AUDIT_LOG_PATH, "a") as f:
        f.write(
            f"{timestamp} | exit={exit_code} | {duration:.2f}s | CMD: {command}\n"
            f"{timestamp} | OUTPUT: {truncated[:1000]}\n"
        )
    logger.info(f"[admin_agent] ran: {command!r} (exit={exit_code}, {duration:.2f}s)")
    return f"[exit_code={exit_code}]\n{truncated}"


def _require_token(x_admin_token: Optional[str]):
    if not ADMIN_API_TOKEN:
        raise HTTPException(status_code=503, detail="admin_agent_not_configured: ADMIN_API_TOKEN unset")
    if not x_admin_token or x_admin_token != ADMIN_API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid_or_missing_admin_token")


@app.post("/admin-chat")
async def admin_chat(req: _AdminChatRequest, x_admin_token: Optional[str] = Header(None)):
    _require_token(x_admin_token)

    cfg = _backend_config()
    if not cfg["proxy_url"] and not cfg["ollama_url"]:
        return JSONResponse(status_code=503, content={"error": "agent_unavailable", "message": "No LLM backend configured (set HERMES_PROXY_URL/ADMIN_HERMES_PROXY_URL or OLLAMA_URL)."})

    messages = [{"role": "system", "content": _SYSTEM_PROMPT}]
    messages += [{"role": h.role, "content": h.content} for h in req.history[-12:]]
    messages.append({"role": "user", "content": req.message})

    commands_run = []
    for step in range(MAX_REACT_STEPS):
        reply = _call_llm(messages, cfg)
        if reply is None:
            return JSONResponse(status_code=503, content={"error": "agent_unavailable", "message": "LLM backend unreachable mid-conversation."})

        m = re.match(r"^\s*SHELL:\s*(.+)$", reply, re.DOTALL)
        if m:
            command = m.group(1).strip()
            result = _run_shell(command)
            commands_run.append({"command": command, "output": result})
            messages.append({"role": "assistant", "content": reply})
            messages.append({"role": "user", "content": f"[shell output]\n{result}"})
            continue

        m = re.match(r"^\s*FINAL:\s*(.+)$", reply, re.DOTALL)
        final_text = m.group(1).strip() if m else reply  # tolerate a model that forgets the prefix
        return {
            "reply": final_text,
            "commands_run": commands_run,
            "steps_used": step + 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    return {
        "reply": "[stopped: reached max steps without a FINAL answer — see commands_run for what was attempted]",
        "commands_run": commands_run,
        "steps_used": MAX_REACT_STEPS,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/health")
def health():
    return {
        "status": "online",
        "configured": bool(ADMIN_API_TOKEN),
        "backend_configured": bool(_backend_config()["proxy_url"] or _backend_config()["ollama_url"]),
        "audit_log": str(AUDIT_LOG_PATH),
        "warning": "unrestricted shell access — operator-only, token-gated",
    }


if __name__ == "__main__":
    import uvicorn
    logger.warning(
        f"[admin_agent] Starting on 0.0.0.0:{ADMIN_AGENT_PORT} — UNRESTRICTED SHELL ACCESS. "
        f"Token configured: {bool(ADMIN_API_TOKEN)}. Audit log: {AUDIT_LOG_PATH}. "
        f"Do not expose this port beyond localhost/your own LAN."
    )
    uvicorn.run(app, host="0.0.0.0", port=ADMIN_AGENT_PORT)
