"""
forge/simulator.py — Paper trading simulation engine

For each incoming candle tick, the simulator:
  1. Checks if any open position hits TP, SL, or timeout
  2. Checks if the new signal triggers a new entry
  3. Logs everything to SQLite via db.py

No money moves. No orders placed. Pure evaluation log.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from forge import db
from forge.strategies import Strategy


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class StrategySimulator:
    """Manages simulation for a single Strategy instance."""

    def __init__(self, strategy: Strategy):
        self.s = strategy
        db.upsert_strategy(strategy.to_dict())

    def on_tick(self, candle: dict):
        """Called for every new candle that matches this strategy's symbol."""
        sym = candle["symbol"]
        if self.s.symbol != "ALL" and self.s.symbol != sym:
            return

        close = candle.get("close")
        atr   = candle.get("atr")
        if not close or not atr or atr == 0:
            return

        # ── 1. Update open positions ──────────────────────────────────────────
        open_trades = db.get_open_trades(self.s.id)
        for trade in open_trades:
            self._update_open(trade, candle)

        # ── 2. Try to open a new position ─────────────────────────────────────
        # Only one position per direction per symbol at a time
        still_open = db.get_open_trades(self.s.id)
        open_dirs = {t["direction"] for t in still_open if t["symbol"] == sym}

        if self.s.direction in ("long", "both") and "long" not in open_dirs:
            conf = candle.get("long_prob", 0)
            if conf and conf >= self.s.entry_threshold:
                self._open_trade(candle, "long", conf)

        if self.s.direction in ("short", "both") and "short" not in open_dirs:
            conf = candle.get("short_prob", 0)
            if conf and conf >= self.s.entry_threshold:
                self._open_trade(candle, "short", conf)

    def _open_trade(self, candle: dict, direction: str, conf: float):
        entry = candle["close"]
        atr   = candle["atr"]
        tp = entry + atr * self.s.tp_atr_mult if direction == "long" \
             else entry - atr * self.s.tp_atr_mult
        sl = entry - atr * self.s.sl_atr_mult if direction == "long" \
             else entry + atr * self.s.sl_atr_mult

        trade_id = db.open_trade({
            "strategy_id":   self.s.id,
            "strategy_name": self.s.name,
            "symbol":        candle["symbol"],
            "direction":     direction,
            "entry_ts":      candle.get("ts", _now()),
            "entry_price":   entry,
            "tp_price":      tp,
            "sl_price":      sl,
            "entry_conf":    conf,
        })
        logger.debug(f"[Forge/{self.s.name}] OPEN {direction} {candle['symbol']} @ {entry:.4f} "
                     f"TP={tp:.4f} SL={sl:.4f} conf={conf:.3f} id={trade_id}")

    def _update_open(self, trade: dict, candle: dict):
        high  = candle.get("high", candle["close"])
        low   = candle.get("low",  candle["close"])
        close = candle["close"]
        ts    = candle.get("ts", _now())

        direction = trade["direction"]
        entry     = trade["entry_price"]
        tp        = trade["tp_price"]
        sl        = trade["sl_price"]
        held      = (trade["candles_held"] or 0) + 1

        exit_price  = None
        exit_reason = None

        if direction == "long":
            if high >= tp:
                exit_price, exit_reason = tp, "tp"
            elif low <= sl:
                exit_price, exit_reason = sl, "sl"
        else:  # short
            if low <= tp:
                exit_price, exit_reason = tp, "tp"
            elif high >= sl:
                exit_price, exit_reason = sl, "sl"

        if exit_reason is None and held >= self.s.max_candles_held:
            exit_price, exit_reason = close, "timeout"

        if exit_reason:
            pnl = (exit_price - entry) / entry if direction == "long" \
                  else (entry - exit_price) / entry
            db.close_trade(trade["id"], {
                "exit_ts":      ts,
                "exit_price":   exit_price,
                "exit_reason":  exit_reason,
                "pnl_pct":      round(pnl * 100, 4),
                "candles_held": held,
            })
            logger.debug(f"[Forge/{self.s.name}] CLOSE {direction} {trade['symbol']} "
                         f"@ {exit_price:.4f} {exit_reason} pnl={pnl*100:.2f}% held={held}")
