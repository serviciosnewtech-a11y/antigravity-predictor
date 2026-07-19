"""
executor/server.py — Antigravity Executor Service
Port 18911

Receives signals from the Predictor, executes orders on exchange via ccxt.
Independent module — communicates with Predictor over REST.

Endpoints:
  GET  /health               — liveness + position summary
  GET  /positions            — all open positions
  POST /execute              — execute a signal (body: ExecuteRequest)
  POST /cancel/{symbol}      — cancel open order for symbol
  POST /close/{symbol}       — market-close position for symbol
  GET  /history              — recent execution history
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

import ccxt
import requests
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from loguru import logger
from pydantic import BaseModel

# ── Config ────────────────────────────────────────────────────────────────────
EXCHANGE_ID      = os.getenv("EXCHANGE", "bybit")
API_KEY          = os.getenv("EXCHANGE_API_KEY", "")
API_SECRET       = os.getenv("EXCHANGE_API_SECRET", "")
PREDICTOR_URL    = os.getenv("PREDICTOR_URL", "http://localhost:18910")
PORT             = int(os.getenv("EXECUTOR_PORT", 18911))
REQUESTED_DRY_RUN = os.getenv("DRY_RUN", "true").lower() in ("1", "true", "yes")
LIVE_CONFIRM      = os.getenv("LIVE_CONFIRM", "")
DRY_RUN           = REQUESTED_DRY_RUN or LIVE_CONFIRM != "I_ACCEPT_LIVE_TRADING"
INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
DASHBOARD_ORIGINS = [
    o.strip() for o in os.getenv("DASHBOARD_ORIGINS", "http://localhost,http://127.0.0.1").split(",")
    if o.strip()
]

# Execution thresholds — ignore signals below these confidence levels
MIN_LONG_CONF    = float(os.getenv("MIN_LONG_CONF",  "0.60"))
MIN_SHORT_CONF   = float(os.getenv("MIN_SHORT_CONF", "0.60"))

# Position sizing — fraction of available balance per trade
POSITION_SIZE_PCT = float(os.getenv("POSITION_SIZE_PCT", "0.02"))   # 2% per trade

# ── Exchange client ───────────────────────────────────────────────────────────
def build_exchange() -> ccxt.Exchange:
    cls = getattr(ccxt, EXCHANGE_ID)
    ex = cls({
        "apiKey":  API_KEY,
        "secret":  API_SECRET,
        "options": {"defaultType": "swap"},
        "enableRateLimit": True,
    })
    return ex


exchange: ccxt.Exchange = None   # initialised in lifespan

# ── State ─────────────────────────────────────────────────────────────────────
history: deque = deque(maxlen=200)    # recent execution records
start_time = time.time()

# ── Models ────────────────────────────────────────────────────────────────────
class ExecuteRequest(BaseModel):
    symbol: str                        # e.g. "BTC/USDT"
    side: str                          # "long" or "short"
    confidence: float                  # model probability
    source: str = "predictor"          # who triggered this
    reason: Optional[str] = None       # narrative from signal agent

class ExecutionRecord(BaseModel):
    timestamp: str
    symbol: str
    side: str
    confidence: float
    action: str           # "placed", "skipped", "error", "dry_run"
    detail: str
    order_id: Optional[str] = None

# ── Helpers ───────────────────────────────────────────────────────────────────
def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

def log_execution(record: ExecutionRecord):
    history.appendleft(record.model_dump())
    logger.info(f"[{record.symbol}] {record.action.upper()} | {record.side} | conf={record.confidence:.3f} | {record.detail}")

def get_balance_usdt() -> float:
    try:
        bal = exchange.fetch_balance()
        return float(bal.get("USDT", {}).get("free", 0))
    except Exception as e:
        logger.warning(f"Could not fetch balance: {e}")
        return 0.0

def calc_order_size(symbol: str, usdt_amount: float) -> float:
    """Convert USDT amount to contract quantity."""
    try:
        ticker = exchange.fetch_ticker(symbol)
        price = ticker["last"]
        markets = exchange.load_markets()
        info = markets.get(symbol, {})
        min_qty = float(info.get("limits", {}).get("amount", {}).get("min", 0.001))
        qty = round(usdt_amount / price, 4)
        return max(qty, min_qty)
    except Exception as e:
        logger.warning(f"Could not calc order size for {symbol}: {e}")
        return 0.0

def require_internal_token(x_internal_token: str = Header(default="")) -> None:
    if not INTERNAL_API_TOKEN:
        raise HTTPException(status_code=503, detail="INTERNAL_API_TOKEN is not configured")
    if x_internal_token != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="invalid internal token")

# ── Lifespan ──────────────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    global exchange
    exchange = build_exchange()
    mode = "DRY RUN" if DRY_RUN else "LIVE"
    logger.info(f"Executor online — exchange={EXCHANGE_ID} mode={mode} port={PORT}")
    if not REQUESTED_DRY_RUN and DRY_RUN:
        logger.warning("DRY_RUN=false ignored: LIVE_CONFIRM must equal I_ACCEPT_LIVE_TRADING to allow live trading.")
    if not DRY_RUN and not API_KEY:
        logger.warning("LIVE mode but EXCHANGE_API_KEY not set — orders will fail.")
    yield
    logger.info("Executor shutting down.")

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="Antigravity Executor", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=DASHBOARD_ORIGINS,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── Routes ────────────────────────────────────────────────────────────────────
@app.get("/health")
def health():
    positions = []
    try:
        positions = exchange.fetch_positions() if not DRY_RUN else []
    except Exception:
        pass
    return {
        "status": "online",
        "dry_run": DRY_RUN,
        "exchange": EXCHANGE_ID,
        "uptime_s": round(time.time() - start_time),
        "open_positions": len(positions),
        "executions_logged": len(history),
        "timestamp": now_iso(),
    }

@app.get("/positions")
def positions():
    if DRY_RUN:
        return {"dry_run": True, "positions": []}
    try:
        pos = exchange.fetch_positions()
        return {"positions": [p for p in pos if float(p.get("contracts", 0)) != 0]}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.post("/execute")
def execute(req: ExecuteRequest, _: None = Depends(require_internal_token)):
    """Execute a signal from the Predictor."""
    min_conf = MIN_LONG_CONF if req.side == "long" else MIN_SHORT_CONF

    # Confidence gate
    if req.confidence < min_conf:
        record = ExecutionRecord(
            timestamp=now_iso(), symbol=req.symbol, side=req.side,
            confidence=req.confidence, action="skipped",
            detail=f"confidence {req.confidence:.3f} below threshold {min_conf:.3f}",
        )
        log_execution(record)
        return record.model_dump()

    # Dry run
    if DRY_RUN:
        record = ExecutionRecord(
            timestamp=now_iso(), symbol=req.symbol, side=req.side,
            confidence=req.confidence, action="dry_run",
            detail=f"DRY RUN — would place {req.side} order",
        )
        log_execution(record)
        return record.model_dump()

    # Live execution
    try:
        balance = get_balance_usdt()
        usdt_amount = balance * POSITION_SIZE_PCT
        qty = calc_order_size(req.symbol, usdt_amount)

        if qty <= 0:
            raise ValueError(f"Calculated qty={qty} — insufficient balance or price fetch failed")

        order_side = "buy" if req.side == "long" else "sell"
        order = exchange.create_market_order(req.symbol, order_side, qty)

        record = ExecutionRecord(
            timestamp=now_iso(), symbol=req.symbol, side=req.side,
            confidence=req.confidence, action="placed",
            detail=f"market order qty={qty} usdt≈{usdt_amount:.2f}",
            order_id=order.get("id"),
        )
        log_execution(record)
        return record.model_dump()

    except Exception as e:
        record = ExecutionRecord(
            timestamp=now_iso(), symbol=req.symbol, side=req.side,
            confidence=req.confidence, action="error", detail=str(e),
        )
        log_execution(record)
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/cancel/{symbol:path}")
def cancel(symbol: str, _: None = Depends(require_internal_token)):
    """Cancel all open orders for a symbol."""
    if DRY_RUN:
        return {"action": "dry_run", "symbol": symbol}
    try:
        orders = exchange.fetch_open_orders(symbol)
        cancelled = []
        for o in orders:
            exchange.cancel_order(o["id"], symbol)
            cancelled.append(o["id"])
        return {"cancelled": cancelled, "symbol": symbol}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.post("/close/{symbol:path}")
def close_position(symbol: str, _: None = Depends(require_internal_token)):
    """Market-close the open position for a symbol."""
    if DRY_RUN:
        return {"action": "dry_run", "symbol": symbol}
    try:
        positions = exchange.fetch_positions([symbol])
        active = [p for p in positions if float(p.get("contracts", 0)) != 0]
        if not active:
            return {"action": "no_position", "symbol": symbol}
        pos = active[0]
        side = "sell" if pos["side"] == "long" else "buy"
        qty = abs(float(pos["contracts"]))
        order = exchange.create_market_order(symbol, side, qty, params={"reduceOnly": True})
        return {"action": "closed", "symbol": symbol, "order_id": order.get("id"), "qty": qty}
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

@app.get("/history")
def get_history(limit: int = 50):
    return {"history": list(history)[:limit]}

# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")
