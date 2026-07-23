"""
src/hermes_persona.py — the shared identity core and durable memory for
"Hermes", the one AI brain behind both the dashboard's interactive chat
(predictor_server.py's /api/chat) and the automated signal-triggered
enrichment (signal_agent/enricher.py). Same personality/boundaries in both
places, and the same memory file: a client's chat about BTC and an
automated signal note about BTC inform each other, rather than running as
two unrelated processes that happen to share a name.

Memory always stores natural-language text, in both directions, regardless
of which surface wrote it. The signal-triggered enrichment's own output is
structured JSON on the wire (the dashboard's Agent Report panel needs
specific fields to render into its UI slots — it never displays raw JSON to
the client), but nothing JSON-shaped ever gets written into memory or fed
back into a prompt: enrichment writes a short natural-language digest of
what it found, the same as a chat exchange would. The client never "speaks
JSON" anywhere in this pipeline.
"""
from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

HERMES_CORE_IDENTITY = """You are Hermes, the AI layer behind the Antigravity Predictor — a self-hosted \
crypto futures signal system (Bybit linear, BTC/ETH/SOL, 15m timeframe, LightGBM long/short models, \
advisory-only, no autonomous execution). You are the same Hermes whether you're chatting directly with a \
client on the dashboard or generating an automated signal note when a threshold fires — same knowledge, \
same boundaries, same memory of what's happened before.

You have no ability to execute trades, place orders, or connect to any exchange, wallet, or account, under \
any circumstance. This is a structural guarantee, not a policy you're choosing to follow — no tool-calling \
is ever wired to you in either context. Never claim to have executed, scheduled, or changed anything. Never \
fabricate data, prices, or sources — state your confidence level explicitly rather than defaulting to \
uniform false confidence."""

_LOGS_DIR = Path(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "logs"))
# Filename predates the chat+enrichment merge (it used to mean "the chat
# persona's memory" specifically) — kept unchanged so existing deployments'
# saved history isn't orphaned by a rename; it's just a shared brain's
# memory file now, written by more than one caller.
MEMORY_PATH = _LOGS_DIR / "crypto_operator_memory.jsonl"

_MEMORY_RECALL = 12       # how many past exchanges to fold back into context
_MEMORY_MAX_LINES = 2000  # rotation cap — well above _MEMORY_RECALL, just bounds disk growth
_lock = threading.Lock()


def memory_recall(path: Optional[Path] = None) -> str:
    """Best-effort read of recent past exchanges, persisted across
    sessions/reloads/restarts and shared across every caller (chat,
    enrichment). Missing/unreadable file just means no memory yet.

    `path` defaults to the module-level MEMORY_PATH, read fresh at call
    time (NOT bound as a function-default value at import time) so that
    tests/callers can monkeypatch hermes_persona.MEMORY_PATH and have it
    actually take effect — a plain `path: Path = MEMORY_PATH` default
    parameter captures the value that existed when this function was
    *defined*, not whatever MEMORY_PATH is when it's *called*."""
    try:
        p = Path(path if path is not None else MEMORY_PATH)
        if not p.exists():
            return ""
        lines = p.read_text().strip().splitlines()[-_MEMORY_RECALL:]
        recalled = []
        for line in lines:
            try:
                rec = json.loads(line)
                recalled.append(f"[{rec.get('ts', '')}] {rec.get('user', '')}\nHermes: {rec.get('agent', '')}")
            except Exception:
                continue
        return "\n\n".join(recalled)
    except Exception:
        return ""


def memory_append(user_msg: str, agent_reply: str, path: Optional[Path] = None) -> None:
    """Append one exchange to the shared memory log. Best-effort — a write
    failure here should never break the caller's actual response. Always
    natural-language text on both sides, never JSON, regardless of whether
    the caller is the interactive chat or the automated enrichment (see
    record_enrichment_digest() below for the latter). See memory_recall()'s
    docstring for why `path` defaults to None and reads MEMORY_PATH fresh
    rather than binding it as a parameter default."""
    p = Path(path if path is not None else MEMORY_PATH)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(json.dumps({
                "ts": datetime.now(timezone.utc).isoformat(),
                "user": user_msg,
                "agent": agent_reply,
            }) + "\n")
        if p.stat().st_size > 0:
            lines = p.read_text().splitlines()
            if len(lines) > _MEMORY_MAX_LINES:
                p.write_text("\n".join(lines[-_MEMORY_MAX_LINES:]) + "\n")
    except Exception as e:
        from loguru import logger
        logger.warning(f"[hermes_persona] memory append failed for {p} (non-fatal): {e}")


def record_enrichment_digest(asset: str, enrichment: dict) -> None:
    """Called by signal_agent/enricher.py after a successful automated
    enrichment. Builds a short natural-language digest from the structured
    JSON result (never writes the JSON itself into memory) and appends it
    as a normal exchange, tagged as automated so a later chat recall reads
    coherently ("[automated BTC/USDT signal check] ... Hermes: ...") rather
    than looking like something the client said."""
    signal = enrichment.get("signal", "NEUTRAL")
    note = enrichment.get("analyst_note", "").strip()
    news = enrichment.get("news_summary", "").strip()
    risks = enrichment.get("key_risks", "").strip()
    digest = " ".join(part for part in [note, news, risks] if part) or "No further detail generated."
    memory_append(
        user_msg=f"[Automated {asset} signal check — model signal: {signal}]",
        agent_reply=digest,
    )
