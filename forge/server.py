"""
forge/server.py — Forge API server   Port 18912

Forge collects live data from the Predictor, paper-trades every registered
strategy in parallel, and logs every outcome to SQLite.

NOTHING promotes automatically. Pull results via API, compare, decide.

Endpoints:
  GET  /health                     — uptime, candles collected, trades logged
  GET  /strategies                 — list active strategies
  POST /strategies                 — register a new strategy
  DELETE /strategies/{id}          — deactivate a strategy
  GET  /leaderboard                — ranked by win_rate / profit_factor
  GET  /results                    — all closed trades (filterable)
  GET  /results/{strategy_id}      — trades for one strategy
  GET  /data/{symbol}              — recent candles with predictions
  GET  /open                       — currently open simulated positions
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from contextlib import asynccontextmanager
from statistics import mean, stdev
from typing import Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

from forge import db
from forge.collector import LiveCollector
from forge.simulator import StrategySimulator
from forge.strategies import DEFAULT_STRATEGIES, Strategy

PORT = int(os.getenv("FORGE_PORT", 18912))

# ── Global state ──────────────────────────────────────────────────────────────
simulators: list[StrategySimulator] = []
candles_received: int = 0
start_time = float(time.time())


# ── Candle fan-out ────────────────────────────────────────────────────────────
def on_candle(candle: dict):
    global candles_received
    candles_received += 1
    for sim in simulators:
        try:
            sim.on_tick(candle)
        except Exception as e:
            logger.warning(f"[Forge] Simulator {sim.s.name} error on tick: {e}")


# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()

    # Load default strategies
    for s in DEFAULT_STRATEGIES:
        simulators.append(StrategySimulator(s))
    logger.success(f"[Forge] {len(simulators)} strategies loaded.")

    # Start WebSocket collector in background
    collector = LiveCollector(on_candle=on_candle)
    task = asyncio.create_task(collector.run())

    logger.success(f"[Forge] Online — port {PORT}")
    yield

    task.cancel()
    logger.info("[Forge] Shutdown.")


# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Forge — Strategy Evaluation Engine", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── Pydantic schemas ──────────────────────────────────────────────────────────
class StrategyRequest(BaseModel):
    name: str
    symbol: str = "BTC/USDT"
    direction: str = "long"
    entry_threshold: float = 0.55
    exit_threshold: float  = 0.40
    tp_atr_mult: float = 1.5
    sl_atr_mult: float = 1.0
    max_candles_held: int = 4
    notes: str = ""


# ── Metrics helper ────────────────────────────────────────────────────────────
def _compute_metrics(trades: list[dict]) -> dict:
    if not trades:
        return {"trade_count": 0}

    pnls      = [t["pnl_pct"] for t in trades if t.get("pnl_pct") is not None]
    wins      = [p for p in pnls if p > 0]
    losses    = [p for p in pnls if p <= 0]
    tp_exits  = sum(1 for t in trades if t.get("exit_reason") == "tp")
    sl_exits  = sum(1 for t in trades if t.get("exit_reason") == "sl")
    to_exits  = sum(1 for t in trades if t.get("exit_reason") == "timeout")

    avg_win  = mean(wins)   if wins   else 0
    avg_loss = mean(losses) if losses else 0
    pf = abs(avg_win / avg_loss) if avg_loss != 0 else float("inf")

    held = [t["candles_held"] for t in trades if t.get("candles_held")]

    return {
        "trade_count":    len(pnls),
        "win_rate":       round(len(wins) / len(pnls) * 100, 1) if pnls else 0,
        "profit_factor":  round(pf, 3),
        "avg_pnl_pct":    round(mean(pnls), 4)  if pnls else 0,
        "total_pnl_pct":  round(sum(pnls), 4)   if pnls else 0,
        "avg_win_pct":    round(avg_win, 4),
        "avg_loss_pct":   round(avg_loss, 4),
        "tp_exits":       tp_exits,
        "sl_exits":       sl_exits,
        "timeout_exits":  to_exits,
        "avg_candles_held": round(mean(held), 1) if held else 0,
        "pnl_stddev":     round(stdev(pnls), 4)  if len(pnls) > 1 else 0,
    }


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {
        "status":            "online",
        "uptime_s":          round(time.time() - start_time),
        "strategies_active": len(simulators),
        "candles_received":  candles_received,
        "trades_logged":     len(db.get_trades(limit=999999)),
        "timestamp":         time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }


@app.get("/strategies")
def list_strategies():
    return {"strategies": [s.s.to_dict() for s in simulators]}


@app.post("/strategies", status_code=201)
def add_strategy(req: StrategyRequest):
    s = Strategy(**req.model_dump())
    sim = StrategySimulator(s)
    simulators.append(sim)
    logger.info(f"[Forge] Added strategy: {s.name} ({s.id})")
    return {"added": s.to_dict()}


@app.delete("/strategies/{strategy_id}")
def remove_strategy(strategy_id: str):
    global simulators
    before = len(simulators)
    simulators = [s for s in simulators if s.s.id != strategy_id]
    if len(simulators) == before:
        raise HTTPException(404, f"Strategy {strategy_id} not found")
    return {"removed": strategy_id}


@app.get("/leaderboard")
def leaderboard(
    symbol: Optional[str]    = None,
    direction: Optional[str] = None,
    min_trades: int          = Query(5, description="Minimum closed trades to include"),
    sort_by: str             = Query("profit_factor", description="win_rate | profit_factor | avg_pnl_pct | total_pnl_pct"),
):
    """
    Ranked strategy comparison. Pull this to decide what to promote.
    Nothing here auto-promotes anything.
    """
    rows = []
    for sim in simulators:
        s = sim.s
        if symbol    and s.symbol    != symbol:    continue
        if direction and s.direction != direction: continue

        trades  = db.get_trades(strategy_id=s.id)
        metrics = _compute_metrics(trades)
        if metrics.get("trade_count", 0) < min_trades:
            continue

        rows.append({
            "strategy_id":   s.id,
            "strategy_name": s.name,
            "symbol":        s.symbol,
            "direction":     s.direction,
            "params": {
                "entry_threshold": s.entry_threshold,
                "tp_atr_mult":     s.tp_atr_mult,
                "sl_atr_mult":     s.sl_atr_mult,
                "max_candles_held": s.max_candles_held,
            },
            **metrics,
        })

    valid_sorts = {"win_rate", "profit_factor", "avg_pnl_pct", "total_pnl_pct"}
    sort_key = sort_by if sort_by in valid_sorts else "profit_factor"
    rows.sort(key=lambda r: r.get(sort_key, 0), reverse=True)

    return {"leaderboard": rows, "sorted_by": sort_key, "total": len(rows)}


@app.get("/results")
def results(
    strategy_id: Optional[str] = None,
    symbol:      Optional[str] = None,
    limit:       int = 200,
):
    trades = db.get_trades(strategy_id=strategy_id, symbol=symbol, limit=limit)
    return {"trades": trades, "count": len(trades)}


@app.get("/results/{strategy_id}")
def results_for_strategy(strategy_id: str, limit: int = 200):
    trades  = db.get_trades(strategy_id=strategy_id, limit=limit)
    metrics = _compute_metrics(trades)
    strat   = next((s.s.to_dict() for s in simulators if s.s.id == strategy_id), None)
    return {"strategy": strat, "metrics": metrics, "trades": trades}


@app.get("/data/{symbol:path}")
def candle_data(symbol: str, limit: int = 100):
    candles = db.get_candles(symbol, limit=limit)
    return {"symbol": symbol, "candles": candles, "count": len(candles)}


@app.get("/open")
def open_positions():
    all_open = []
    for sim in simulators:
        open_trades = db.get_open_trades(sim.s.id)
        for t in open_trades:
            t["strategy_name"] = sim.s.name
            all_open.append(t)
    return {"open_positions": all_open, "count": len(all_open)}


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
