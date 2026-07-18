"""
forge/collector.py — Real-time data collector

Subscribes to the Predictor's WebSocket feed.
For each incoming candle, computes ATR, stores the candle,
and fans it out to all active strategy simulators.
"""

from __future__ import annotations

import asyncio
import json
import os
from collections import defaultdict, deque
from typing import Callable

import websockets
from loguru import logger

PREDICTOR_WS = os.getenv("PREDICTOR_WS_URL", "ws://predictor:18910/ws")
ATR_PERIOD    = int(os.getenv("ATR_PERIOD", "14"))
RECONNECT_S   = int(os.getenv("WS_RECONNECT_S", "5"))


def _compute_atr(history: deque, period: int = 14) -> float | None:
    """Wilder ATR over recent candles."""
    if len(history) < 2:
        return None
    trs = []
    h = list(history)
    for i in range(1, len(h)):
        high  = h[i].get("high",  h[i]["close"])
        low   = h[i].get("low",   h[i]["close"])
        prev_close = h[i - 1]["close"]
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)
    if not trs:
        return None
    trs = trs[-period:]
    return sum(trs) / len(trs)


class LiveCollector:
    """
    Connects to Predictor WS, enriches candles with ATR + model predictions,
    and calls on_candle(candle_dict) for every completed tick.
    """

    def __init__(self, on_candle: Callable[[dict], None]):
        self.on_candle = on_candle
        self._history: dict[str, deque] = defaultdict(lambda: deque(maxlen=ATR_PERIOD + 5))
        self._running = False

    async def run(self):
        self._running = True
        while self._running:
            try:
                await self._connect()
            except Exception as e:
                logger.warning(f"[Collector] WS error: {e} — reconnecting in {RECONNECT_S}s")
                await asyncio.sleep(RECONNECT_S)

    async def _connect(self):
        logger.info(f"[Collector] Connecting to {PREDICTOR_WS}")
        async with websockets.connect(PREDICTOR_WS, ping_interval=20) as ws:
            logger.success("[Collector] Connected to Predictor WebSocket.")
            async for raw in ws:
                try:
                    msg = json.loads(raw)
                    await self._handle(msg)
                except Exception as e:
                    logger.debug(f"[Collector] Parse error: {e}")

    async def _handle(self, msg: dict):
        # Predictor WS emits: {type: "tick", symbol: ..., close: ..., high: ..., low: ...,
        #                       long_prob: ..., short_prob: ..., trend: ..., ts: ...}
        if msg.get("type") != "tick":
            return

        sym   = msg.get("symbol")
        close = msg.get("close") or msg.get("price")
        if not sym or not close:
            return

        candle = {
            "symbol":     sym,
            "ts":         msg.get("ts", ""),
            "open":       msg.get("open",  close),
            "high":       msg.get("high",  close),
            "low":        msg.get("low",   close),
            "close":      float(close),
            "volume":     msg.get("volume", 0),
            "long_prob":  msg.get("long_prob",  0),
            "short_prob": msg.get("short_prob", 0),
            "trend":      msg.get("trend", ""),
        }

        self._history[sym].append(candle)
        candle["atr"] = _compute_atr(self._history[sym], ATR_PERIOD)

        # Store + fan out (synchronously — collectors and simulators are in the same thread pool)
        from forge import db
        db.insert_candle(candle)
        self.on_candle(candle)

    def stop(self):
        self._running = False
