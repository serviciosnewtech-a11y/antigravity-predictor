"""
src/llm_backend.py — shared backend resolution + LLM-calling logic for every
Hermes-branded AI surface in this system: the dashboard's interactive chat
(predictor_server.py's /api/chat) and the signal-triggered enrichment
(signal_agent/enricher.py). One backend-selection mechanism (CHAT_BACKEND),
one HTTP client implementation per backend type -- before this module
existed, predictor_server.py's chat and signal_agent/enricher.py's
enrichment each independently reimplemented "call an OpenAI-compatible /
Anthropic / Ollama endpoint", with two different backend-selection env
vars (CHAT_BACKEND vs SA_INFERENCE_BACKEND) that had already drifted (no
native Anthropic support in the enrichment path at all). "Same brain, one
backend" means one implementation, used by both.

This module has zero opinion about what a caller's prompt says or what it
does with the reply -- system prompt construction and reply parsing (a
structured JSON signal brief vs. free conversational text) stay entirely
with each caller. This module only resolves "which backend" and "how do I
speak that backend's wire protocol."
"""
from __future__ import annotations

import os
from typing import Optional

import requests
from loguru import logger


def backend_config() -> dict:
    """
    Resolve chat-backend config from env. Three backend types are supported
    out of the box:

      hermes_proxy — any OpenAI-compatible /chat/completions endpoint (this
                     already covers most third-party agents via a thin
                     relay, see .env.example's Pattern A/B)
      anthropic    — native Anthropic Messages API, for a client who wants
                     to point straight at Claude without an OpenAI-compat
                     shim in front of it
      ollama       — local Ollama server

    CHAT_BACKEND, if set, forces exactly one and skips the others entirely
    (an explicit choice that's misconfigured should surface as unavailable,
    not silently fail over to a different backend the caller didn't ask
    for). Left unset, the order in call_llm() is tried automatically, for
    backward compatibility with pre-existing HERMES_PROXY_URL-only
    deployments.
    """
    return {
        "backend_override": os.environ.get("CHAT_BACKEND", "").strip().lower() or None,
        "proxy_url": os.environ.get("HERMES_PROXY_URL", "").rstrip("/"),
        "proxy_key": os.environ.get("HERMES_PROXY_API_KEY", "local"),
        "proxy_model": os.environ.get("HERMES_INFERENCE_MODEL", "gemma4:12b-it-qat-policy-128k"),
        "anthropic_key": os.environ.get("ANTHROPIC_API_KEY", ""),
        "anthropic_model": os.environ.get("ANTHROPIC_CHAT_MODEL", "claude-sonnet-5"),
        "ollama_url": os.environ.get("OLLAMA_URL", "").rstrip("/"),
        "ollama_model": os.environ.get("OLLAMA_MODEL", "llama3.2"),
    }


def _openai_style_messages(system_ctx: str, user_content: str, messages_tail: list) -> list:
    messages = [{"role": "system", "content": system_ctx}] + list(messages_tail)
    messages.append({"role": "user", "content": user_content})
    return messages


def call_hermes_proxy(cfg: dict, system_ctx: str, user_content: str,
                       messages_tail: Optional[list] = None, max_tokens: Optional[int] = None) -> Optional[dict]:
    if not cfg["proxy_url"]:
        return None
    try:
        payload = {
            "model": cfg["proxy_model"],
            "messages": _openai_style_messages(system_ctx, user_content, messages_tail or []),
            "stream": False,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        resp = requests.post(
            f"{cfg['proxy_url']}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {cfg['proxy_key']}"},
            timeout=60,
        )
        if resp.status_code == 200:
            choices = resp.json().get("choices", [])
            if choices and "message" in choices[0]:
                message = choices[0]["message"]
                reply = message.get("content") or message.get("reasoning") or ""
                if isinstance(reply, list):
                    reply = "".join(str(p.get("text", p)) if isinstance(p, dict) else str(p) for p in reply)
                reply = str(reply).strip()
                if reply:
                    return {"reply": reply, "source": "hermes_proxy"}
    except Exception as e:
        logger.warning(f"[llm_backend] Hermes Proxy unavailable: {e}")
    return None


def call_anthropic(cfg: dict, system_ctx: str, user_content: str,
                    messages_tail: Optional[list] = None, max_tokens: Optional[int] = None) -> Optional[dict]:
    """Native Anthropic Messages API — distinct wire shape from the OpenAI-
    compatible proxy path: system prompt is a top-level param (not a
    "system" role inside messages[]), auth is x-api-key + anthropic-version
    headers (not Bearer), and the reply is at content[0]["text"], not
    choices[0]["message"]["content"]."""
    if not cfg["anthropic_key"]:
        return None
    try:
        messages = list(messages_tail or []) + [{"role": "user", "content": user_content}]
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            json={
                "model": cfg["anthropic_model"],
                "system": system_ctx,
                "messages": messages,
                "max_tokens": max_tokens or 1024,
            },
            headers={
                "x-api-key": cfg["anthropic_key"],
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            timeout=60,
        )
        if resp.status_code == 200:
            content = resp.json().get("content", [])
            if content and content[0].get("type") == "text":
                reply = content[0].get("text", "").strip()
                if reply:
                    return {"reply": reply, "source": "anthropic"}
    except Exception as e:
        logger.warning(f"[llm_backend] Anthropic unavailable: {e}")
    return None


def call_ollama(cfg: dict, system_ctx: str, user_content: str,
                messages_tail: Optional[list] = None, max_tokens: Optional[int] = None) -> Optional[dict]:
    if not cfg["ollama_url"]:
        return None
    try:
        resp = requests.post(
            f"{cfg['ollama_url']}/api/chat",
            json={
                "model": cfg["ollama_model"],
                "messages": _openai_style_messages(system_ctx, user_content, messages_tail or []),
                "stream": False,
            },
            timeout=60,
        )
        if resp.status_code == 200:
            reply = resp.json().get("message", {}).get("content", "").strip()
            if reply:
                return {"reply": reply, "source": "ollama"}
    except Exception as e:
        logger.warning(f"[llm_backend] Ollama unavailable: {e}")
    return None


_CALLERS = {"hermes_proxy": call_hermes_proxy, "anthropic": call_anthropic, "ollama": call_ollama}


def call_llm(system_ctx: str, user_content: str, messages_tail: Optional[list] = None,
             cfg: Optional[dict] = None, max_tokens: Optional[int] = None) -> Optional[dict]:
    """
    The one dispatcher every Hermes surface calls. If CHAT_BACKEND names a
    backend explicitly, only that one is tried — a failure or missing
    config there returns None rather than falling through to a different
    backend. Left unset, tries hermes_proxy, then anthropic, then ollama (an
    order that predates anthropic support, kept for backward compatibility
    with existing HERMES_PROXY_URL-only deployments). Returns a reply dict
    ({"reply": ..., "source": ...}) on success, or None if nothing
    configured/reachable — callers turn that into an honest unavailable
    state, never a scripted fallback reply.
    """
    cfg = cfg or backend_config()
    messages_tail = messages_tail or []
    override = cfg.get("backend_override")

    if override:
        caller = _CALLERS.get(override)
        if not caller:
            logger.warning(
                f"[llm_backend] CHAT_BACKEND={override!r} not recognized "
                f"(expected hermes_proxy|anthropic|ollama) — no backend called"
            )
            return None
        return caller(cfg, system_ctx, user_content, messages_tail, max_tokens)

    for name in ("hermes_proxy", "anthropic", "ollama"):
        result = _CALLERS[name](cfg, system_ctx, user_content, messages_tail, max_tokens)
        if result:
            return result
    return None
