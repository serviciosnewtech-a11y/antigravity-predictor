"""
tests/test_chat_does_not_block_event_loop.py — regression test for a real
live bug found 2026-07-23: /api/chat's route handler (hermes_chat()) was
`async def`, but the backend call underneath it (_call_llm_backend() ->
llm_backend.call_llm() -> requests.post(..., timeout=60)) is synchronous,
blocking I/O -- called directly, with no await, no thread offload.

Every backend used during development (an echo stub, a direct proxy call)
replied in milliseconds, so this was invisible in every prior test and
every prior manual check. Once a real agent was wired in -- one that takes
actual seconds to think, not milliseconds -- that same blocking call
stalled uvicorn's entire event loop for the duration of every /api/chat
request. With the loop stalled, other concurrent async work on the same
process (the dashboard's WebSocket connection) got starved, and the
resulting interleaving corruption surfaced as
`h11._util.LocalProtocolError: Too much data for declared Content-Length`
-- a transport-level symptom that looked completely unrelated to this
endpoint. Confirmed NOT to be a response-encoding issue (reproduced
predictor's exact response shape, including real accented Spanish text,
under a live uvicorn server with no errors) before landing on "the handler
blocks the event loop" as the actual explanation.

Fix: hermes_chat() is now a thin async wrapper that runs the real work
(_hermes_chat_sync(), unchanged logic) via asyncio.to_thread(), freeing the
event loop to keep servicing other connections while a slow backend call is
in flight.

This test proves the property that actually matters -- event loop
responsiveness during a slow backend call -- rather than just asserting the
code happens to call asyncio.to_thread somewhere. It runs a real asyncio
event loop, starts hermes_chat() against a backend mock that blocks
(synchronously, via time.sleep -- exactly like the real requests.post(...)
call it stands in for) for a noticeable duration, and concurrently runs a
lightweight async "heartbeat" task that increments a counter every 10ms.
Before the fix, the blocking call ties up the only thread the event loop
runs on, and the heartbeat can't advance until hermes_chat() returns. After
the fix, the blocking call runs in a worker thread and the heartbeat keeps
ticking throughout.
"""
import asyncio
import os
import sys
import threading
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import predictor_server as ps  # noqa: E402


class _FakeEngine:
    def __init__(self):
        self.lock = threading.Lock()
        self.latest_signal = "BUY"
        self.latest_prediction_long = 0.06
        self.latest_prediction_short = 0.05
        self.position = None
        self.latest_close = 65000.0
        self.latest_atr = 140.0
        self.degraded = False
        self.missing_features = []
        self.inference_blocked_count = 0

    def _stats(self):
        return {"total_trades": 0, "win_trades": 0, "loss_trades": 0, "total_pnl": 0.0}


def _slow_backend_call(system_ctx, message, history, cfg, sleep_s=0.3):
    """Stands in for llm_backend.call_llm()'s real synchronous
    requests.post(..., timeout=60) -- a real agent taking real seconds to
    think looks exactly like this from the event loop's perspective: a
    blocking call on the calling thread, no awaiting involved."""
    time.sleep(sleep_s)
    return {"reply": "respuesta de prueba", "source": "hermes_proxy"}


def test_hermes_chat_does_not_block_event_loop_during_slow_backend_call(monkeypatch):
    monkeypatch.setattr(ps, "engines", {"BTC/USDT": _FakeEngine()})
    monkeypatch.setattr(ps, "_enriched_signals", {})
    monkeypatch.setattr(ps, "_operator_memory_recall", lambda: "")
    monkeypatch.setattr(ps, "_operator_memory_append", lambda *a, **kw: None)
    monkeypatch.setattr(ps, "_live_system_context", lambda: "no live context in test")
    monkeypatch.setattr(ps, "_model_performance_context", lambda: "no model context in test")
    monkeypatch.setattr(ps, "_backend_config", lambda: {})
    monkeypatch.setattr(ps, "_call_llm_backend", _slow_backend_call)

    req = ps._ChatRequest(message="ping", symbol="BTC/USDT", language="es", history=[])

    heartbeat_ticks = []

    async def heartbeat():
        for _ in range(60):  # 60 * 10ms = 600ms of observation window
            heartbeat_ticks.append(time.monotonic())
            await asyncio.sleep(0.01)

    async def run_both():
        heartbeat_task = asyncio.create_task(heartbeat())
        chat_task = asyncio.create_task(ps.hermes_chat(req))
        result = await chat_task
        heartbeat_task.cancel()
        return result

    result = asyncio.run(run_both())

    assert result["reply"] == "respuesta de prueba"

    # The real assertion: the heartbeat must have kept ticking *during* the
    # 300ms blocking backend call, not just before/after it. If the event
    # loop were blocked (the pre-fix behavior), most or all heartbeat ticks
    # would bunch up immediately before hermes_chat() started or immediately
    # after it returned, rather than being spread evenly across the whole
    # window -- concretely, we'd see far fewer than ~25-30 ticks land within
    # the ~300ms the backend call was sleeping.
    assert len(heartbeat_ticks) >= 20, (
        f"only {len(heartbeat_ticks)} heartbeat ticks recorded during a "
        f"~300ms blocking backend call -- the event loop was stalled, "
        f"the same failure mode that produced the live h11 Content-Length "
        f"error under real (non-instant) backend latency"
    )
