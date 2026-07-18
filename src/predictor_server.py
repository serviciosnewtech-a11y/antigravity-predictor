"""
Antigravity Predictor Server — v2  (BTC / ETH / SOL multi-asset)
"""
import os, json, asyncio, threading, time
import requests
import numpy as np
import pandas as pd
import lightgbm as lgb
from datetime import datetime, timezone
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query, HTTPException
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from loguru import logger
import websockets
from typing import Optional, List

# ── Config ───────────────────────────────────────────────────────────────────
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
try:
    with open(CONFIG_PATH) as f:
        config = json.load(f)
except Exception as e:
    logger.error(f"Failed to load config: {e}")
    raise

ASSETS = list(config["assets"].keys())          # ["BTC/USDT", "ETH/USDT", "SOL/USDT"]
TIMEFRAME = config.get("timeframe", "15m")
TF_MINS = {"1m": 1, "5m": 5, "15m": 15, "30m": 30, "1h": 60}.get(TIMEFRAME, 15)

# ── WebSocket Connection Manager ─────────────────────────────────────────────
class ConnectionManager:
    def __init__(self):
        self.active: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self.active.append(ws)

    def disconnect(self, ws: WebSocket):
        self.active.discard(ws) if hasattr(self.active, "discard") else None
        if ws in self.active:
            self.active.remove(ws)

    async def broadcast(self, msg: dict):
        dead = []
        for ws in self.active:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)

manager = ConnectionManager()

# ── Feature Engineering (shared across all assets) ───────────────────────────
def build_features(candles: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(candles)
    df["date"] = pd.to_datetime(df["time"], unit="s")
    out = df.sort_values("date").reset_index(drop=True)

    close_val = out["close"].replace(0, pd.NA)
    out["log_return_1"] = np.log(out["close"] / out["close"].shift(1).replace(0, pd.NA)).fillna(0.0)
    out["log_return_3"] = np.log(out["close"] / out["close"].shift(3).replace(0, pd.NA)).fillna(0.0)
    out["log_return_6"] = np.log(out["close"] / out["close"].shift(6).replace(0, pd.NA)).fillna(0.0)

    range_raw = out["high"] - out["low"]
    out["range_1"]  = (range_raw / close_val).fillna(0.0)
    out["body"]     = (out["close"] - out["open"]).abs()
    out["atr_proxy"] = range_raw.rolling(14, min_periods=5).mean().fillna(0.0)
    out["volatility_lookback"] = out["log_return_1"].rolling(20, min_periods=5).std().fillna(0.0)

    out["hour_of_day"]   = out["date"].dt.hour.fillna(0).astype(int)
    out["day_of_week"]   = out["date"].dt.dayofweek.fillna(0).astype(int)
    out["session_asia"]    = out["hour_of_day"].between(0, 7).astype(int)
    out["session_london"]  = out["hour_of_day"].between(7, 13).astype(int)
    out["session_newyork"] = out["hour_of_day"].between(13, 21).astype(int)

    out["vol_mean_20"] = out["volume"].rolling(20, min_periods=5).mean().fillna(1.0)
    out["vol_std_20"]  = out["volume"].rolling(20, min_periods=5).std().fillna(0.0)
    out["volume_zscore"]    = ((out["volume"] - out["vol_mean_20"]) / out["vol_std_20"].replace(0, pd.NA)).fillna(0.0)
    out["relative_volume"]  = (out["volume"] / out["vol_mean_20"]).fillna(1.0)
    vol_min = out["volume"].rolling(100, min_periods=10).min()
    vol_max = out["volume"].rolling(100, min_periods=10).max()
    out["volume_percentile"] = ((out["volume"] - vol_min) / (vol_max - vol_min).replace(0, pd.NA)).fillna(0.5)

    range_safe = range_raw.replace(0, pd.NA)
    out["body_ratio"]       = (out["body"] / range_safe).fillna(0.0)
    close_open_max = out[["close", "open"]].max(axis=1)
    close_open_min = out[["close", "open"]].min(axis=1)
    out["upper_wick_ratio"] = ((out["high"] - close_open_max) / range_safe).fillna(0.0)
    out["lower_wick_ratio"] = ((close_open_min - out["low"]) / range_safe).fillna(0.0)
    out["atr_normalized_range"] = (range_raw / out["atr_proxy"].replace(0, pd.NA)).fillna(0.0)
    out["stop_distance"]    = (out["atr_proxy"] / close_val).fillna(0.0)

    out["ema_fast"]       = out["close"].ewm(span=9, adjust=False).mean()
    out["ema_slow"]       = out["close"].ewm(span=21, adjust=False).mean()
    out["dist_ema_fast"]  = ((out["close"] - out["ema_fast"]) / close_val).fillna(0.0)
    out["dist_ema_slow"]  = ((out["close"] - out["ema_slow"]) / close_val).fillna(0.0)
    out["trend_strength"] = ((out["ema_fast"] - out["ema_slow"]) / close_val).fillna(0.0)
    out["trend_direction"] = (out["ema_fast"] > out["ema_slow"]).astype(int) - (out["ema_fast"] < out["ema_slow"]).astype(int)
    out["ema_slow_slope"]  = ((out["ema_slow"] - out["ema_slow"].shift(1)) / out["ema_slow"].replace(0, pd.NA)).fillna(0.0)

    atr_safe   = out["atr_proxy"].replace(0, pd.NA)
    prev_high  = out["high"].shift(1).rolling(20, min_periods=5).max()
    prev_low   = out["low"].shift(1).rolling(20, min_periods=5).min()
    out["sweep_high_detected"] = ((out["high"] > prev_high) & (out["close"] < prev_high)).astype(int)
    out["sweep_low_detected"]  = ((out["low"] < prev_low)  & (out["close"] > prev_low)).astype(int)
    out["sweep_depth_atr"] = (
        ((out["high"] - prev_high) * out["sweep_high_detected"] + (prev_low - out["low"]) * out["sweep_low_detected"])
        / atr_safe
    ).fillna(0.0)
    out["sweep_rejection_ratio"]  = (out["sweep_high_detected"] * out["upper_wick_ratio"] + out["sweep_low_detected"] * out["lower_wick_ratio"])
    out["sweep_volume_zscore"]     = ((out["sweep_high_detected"] + out["sweep_low_detected"]) * out["volume_zscore"])

    high_2 = out["high"].shift(2)
    low_2  = out["low"].shift(2)
    out["bullish_fvg_present"] = (out["low"]  > high_2).astype(int)
    out["bearish_fvg_present"] = (out["high"] < low_2).astype(int)
    out["fvg_size_atr"] = (
        ((out["low"] - high_2) * out["bullish_fvg_present"] + (low_2 - out["high"]) * out["bearish_fvg_present"])
        / atr_safe
    ).fillna(0.0)
    fvg_event = (out["bullish_fvg_present"] | out["bearish_fvg_present"])
    fvg_group = fvg_event.cumsum()
    out["fvg_age_candles"] = out.groupby(fvg_group).cumcount()
    out.loc[fvg_group == 0, "fvg_age_candles"] = 999.0
    prev_bullish_fvg = out["bullish_fvg_present"].shift(1).fillna(0).astype(bool)
    prev_bearish_fvg = out["bearish_fvg_present"].shift(1).fillna(0).astype(bool)
    out["price_inside_fvg"] = 0
    out.loc[prev_bullish_fvg & (out["close"] < out["low"].shift(1)) & (out["close"] > high_2.shift(1)), "price_inside_fvg"] = 1
    out.loc[prev_bearish_fvg & (out["close"] > out["high"].shift(1)) & (out["close"] < low_2.shift(1)), "price_inside_fvg"] = -1

    out["breakout_volume_confirmation"] = (out["volume_zscore"] * (out["close"] > out["close"].shift(1)).astype(int)).clip(lower=0.0).fillna(0.0)
    out["rejection_volume_confirmation"] = (out["volume_zscore"] * (out["upper_wick_ratio"] + out["lower_wick_ratio"])).clip(lower=0.0).fillna(0.0)

    vol_mean = out["volume"].rolling(20, min_periods=5).mean()
    vol_std  = out["volume"].rolling(20, min_periods=5).std().replace(0, pd.NA)
    vol_z    = ((out["volume"] - vol_mean) / vol_std).fillna(0.0)
    body_quality = (out["body_ratio"].fillna(0.0) * out["range_1"].fillna(0.0)).clip(lower=0.0)
    out["volume_block_strength"] = (vol_z.clip(lower=0.0) * (1.0 + body_quality)).fillna(0.0)

    atr_min = out["atr_proxy"].rolling(100, min_periods=10).min()
    atr_max = out["atr_proxy"].rolling(100, min_periods=10).max()
    out["atr_percentile"] = ((out["atr_proxy"] - atr_min) / (atr_max - atr_min).replace(0, pd.NA)).fillna(0.5)
    rolling_max_20 = out["high"].rolling(20).max()
    rolling_min_20 = out["low"].rolling(20).min()
    out["range_compression"]   = (out["atr_proxy"] / (rolling_max_20 - rolling_min_20).replace(0, pd.NA)).fillna(0.0)
    out["high_volatility_flag"] = (out["atr_proxy"] > out["atr_proxy"].rolling(50).median().fillna(0.0)).astype(int)

    out["market_regime"] = 0
    out.loc[(out["trend_strength"] > 0.002)  & (out["volatility_lookback"] > 0), "market_regime"] = 1
    out.loc[(out["trend_strength"] < -0.002) & (out["volatility_lookback"] > 0), "market_regime"] = -1

    # alias columns that some models may expect
    for exp, calc in [
        ("return_1", "log_return_1"), ("return_3", "log_return_3"), ("return_6", "log_return_6"),
        ("liquidity_sweep_up", "sweep_high_detected"), ("liquidity_sweep_down", "sweep_low_detected"),
        ("fvg_bullish", "bullish_fvg_present"), ("fvg_bearish", "bearish_fvg_present"),
    ]:
        if calc in out.columns and exp not in out.columns:
            out[exp] = out[calc]

    return out


# ── Per-Asset Predictor Engine ────────────────────────────────────────────────
class AssetEngine:
    def __init__(self, symbol: str, cfg: dict):
        self.symbol  = symbol
        self.cfg     = cfg
        self.model_long  = None
        self.model_short = None
        self.feature_names: list[str] = []
        self.candles: list[dict] = []
        self.latest_prediction_long  = 0.0
        self.latest_prediction_short = 0.0
        self.latest_signal = "NEUTRAL"
        self.position = None
        self.trades_history: list[dict] = []
        self.total_pnl   = 0.0
        self.win_trades  = 0
        self.loss_trades = 0
        self.lock = threading.Lock()

    def load_models(self):
        logger.info(f"[{self.symbol}] Loading Long model: {self.cfg['model_long_path']}")
        logger.info(f"[{self.symbol}] Loading Short model: {self.cfg['model_short_path']}")
        self.model_long  = lgb.Booster(model_file=self.cfg["model_long_path"])
        self.model_short = lgb.Booster(model_file=self.cfg["model_short_path"])
        self.feature_names = self.model_long.feature_name()
        logger.success(f"[{self.symbol}] Models loaded — {len(self.feature_names)} features.")

    def fetch_initial_candles(self):
        sym = self.symbol.replace("/", "")
        url = f"https://api.bybit.com/v5/market/kline?category=linear&symbol={sym}&interval={TF_MINS}&limit=150"
        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get("retCode") == 0:
                rows = data["result"]["list"]
                rows.reverse()
                with self.lock:
                    self.candles = [
                        {"time": int(row[0]) // 1000, "open": float(row[1]),
                         "high": float(row[2]), "low": float(row[3]),
                         "close": float(row[4]), "volume": float(row[5])}
                        for row in rows
                    ]
                logger.success(f"[{self.symbol}] Loaded {len(self.candles)} historical candles.")
            else:
                logger.error(f"[{self.symbol}] Bybit error: {data}")
        except Exception as e:
            logger.error(f"[{self.symbol}] fetch_initial_candles: {e}")

    def update_candle(self, ts, o, h, l, c, v, confirm, loop):
        with self.lock:
            if not self.candles:
                return
            if self.candles[-1]["time"] == ts:
                self.candles[-1].update({"open": o, "high": h, "low": l, "close": c, "volume": v})
            elif ts > self.candles[-1]["time"]:
                self.candles.append({"time": ts, "open": o, "high": h, "low": l, "close": c, "volume": v})
                if len(self.candles) > 160:
                    self.candles.pop(0)
            self._run_prediction(confirm, loop)

    def _run_prediction(self, confirm, loop):
        if not self.model_long or not self.model_short or len(self.candles) < 50:
            return
        try:
            feats = build_features(self.candles)

            # optional derived features
            if "fvg_bullish_strength" in self.feature_names and "fvg_bullish_strength" not in feats.columns:
                feats["fvg_bullish_strength"] = feats["fvg_size_atr"] * feats["bullish_fvg_present"]
            if "fvg_bearish_strength" in self.feature_names and "fvg_bearish_strength" not in feats.columns:
                feats["fvg_bearish_strength"] = feats["fvg_size_atr"] * feats["bearish_fvg_present"]

            for col in self.feature_names:
                if col not in feats.columns:
                    feats[col] = 0.0

            X = feats[self.feature_names].fillna(0.0).replace([pd.NA, float("inf"), float("-inf")], 0.0).astype(float)
            self.latest_prediction_long  = float(self.model_long.predict(X)[-1])
            self.latest_prediction_short = float(self.model_short.predict(X)[-1])

            # Signal logic
            old_sig = self.latest_signal
            if not self.position:
                if self.latest_prediction_long  >= self.cfg["buy_threshold"]:
                    self.latest_signal = "BUY"
                elif self.latest_prediction_short >= self.cfg["sell_threshold"]:
                    self.latest_signal = "SELL"
                else:
                    self.latest_signal = "NEUTRAL"
            else:
                if self.position["type"] == "LONG" and self.latest_prediction_long < self.cfg["exit_threshold"]:
                    self.latest_signal = "EXIT"
                elif self.position["type"] == "SHORT" and self.latest_prediction_short < self.cfg["exit_short_threshold"]:
                    self.latest_signal = "EXIT"
                else:
                    self.latest_signal = "NEUTRAL"

            last = feats.iloc[-1]
            self._update_sim(float(last["close"]), last["time"], float(last["atr_proxy"]), confirm)

            asyncio.run_coroutine_threadsafe(
                manager.broadcast({
                    "type": "tick",
                    "symbol": self.symbol,
                    "candle": self.candles[-1],
                    "prediction_long":  self.latest_prediction_long,
                    "prediction_short": self.latest_prediction_short,
                    "signal": self.latest_signal,
                    "position": self.position,
                    "stats": self._stats(),
                }),
                loop,
            )
            if confirm and old_sig != self.latest_signal:
                logger.info(f"[{self.symbol}] Signal: {self.latest_signal} | L={self.latest_prediction_long:.4f} S={self.latest_prediction_short:.4f}")
        except Exception as ex:
            logger.error(f"[{self.symbol}] prediction error: {ex}")

    def _update_sim(self, price, ts, atr, confirm):
        spread = price * self.cfg["spread_offset_pct"]
        bid    = price - spread / 2
        ask    = price + spread / 2
        if self.position:
            pos = self.position
            if pos["type"] == "LONG":
                exit_price = bid
                pnl = (bid - pos["entry_price"]) / pos["entry_price"]
                hit_tp = bid >= pos["tp"]
                hit_sl = bid <= pos["sl"]
            else:
                exit_price = ask
                pnl = (pos["entry_price"] - ask) / pos["entry_price"]
                hit_tp = ask <= pos["tp"]
                hit_sl = ask >= pos["sl"]

            reason = None
            if hit_tp:
                reason = "Take Profit"
            elif hit_sl:
                reason = "Stop Loss"
            elif self.latest_signal == "EXIT":
                reason = "Exit Signal"

            if confirm:
                pos["candles_held"] += 1
                if pos["candles_held"] >= self.cfg.get("max_candles_held", 4):
                    reason = "Time Decay"

            if reason:
                pnl_usdt = 100.0 * pnl
                self.total_pnl += pnl_usdt
                if pnl >= 0: self.win_trades += 1
                else: self.loss_trades += 1
                ts_int = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
                self.trades_history.append({
                    "symbol": self.symbol,
                    "type": pos["type"],
                    "entry_time": pos["entry_time"],
                    "exit_time": ts_int,
                    "entry_price": pos["entry_price"],
                    "exit_price": exit_price,
                    "pnl": pnl_usdt,
                    "pnl_pct": pnl,
                    "reason": reason,
                })
                logger.success(f"[{self.symbol}] {pos['type']} EXIT | {reason} | PnL {pnl_usdt:+.2f} USDT")
                self.position = None
        elif confirm:
            ts_int = int(ts.timestamp()) if hasattr(ts, "timestamp") else int(ts)
            if self.latest_signal == "BUY":
                self.position = {
                    "type": "LONG", "entry_time": ts_int, "entry_price": ask,
                    "tp": ask + self.cfg["tp_atr_mult"] * atr,
                    "sl": ask - self.cfg["sl_atr_mult"] * atr, "candles_held": 0,
                }
            elif self.latest_signal == "SELL":
                self.position = {
                    "type": "SHORT", "entry_time": ts_int, "entry_price": bid,
                    "tp": bid - self.cfg["tp_atr_mult"] * atr,
                    "sl": bid + self.cfg["sl_atr_mult"] * atr, "candles_held": 0,
                }

    def _stats(self):
        return {
            "total_pnl": self.total_pnl,
            "win_trades": self.win_trades,
            "loss_trades": self.loss_trades,
            "total_trades": len(self.trades_history),
        }

    def snapshot(self):
        """Thread-safe snapshot for REST endpoints."""
        with self.lock:
            return {
                "symbol": self.symbol,
                "status": "online",
                "latest_prediction_long":  self.latest_prediction_long,
                "latest_prediction_short": self.latest_prediction_short,
                "latest_signal": self.latest_signal,
                "position": self.position,
                "candles": list(self.candles),
                "trades": list(self.trades_history),
                "stats": self._stats(),
            }


# ── Create engines ────────────────────────────────────────────────────────────
engines: dict[str, AssetEngine] = {
    sym: AssetEngine(sym, cfg) for sym, cfg in config["assets"].items()
}

# ── Enriched signal store (written by the Hermes signal agent) ────────────────
# Keyed by normalised symbol, e.g. "BTC/USDT"
_enriched_signals: dict[str, dict] = {}

# ── FastAPI App ───────────────────────────────────────────────────────────────
app = FastAPI(title="Antigravity Predictor v2")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True,
                   allow_methods=["*"], allow_headers=["*"])

loop: asyncio.AbstractEventLoop | None = None

# ── Bybit WebSocket listener (one connection, all symbols) ────────────────────
async def poll_bybit_websocket():
    ws_url = "wss://stream.bybit.com/v5/public/linear"
    topics = [f"kline.{TF_MINS}.{sym.replace('/', '')}" for sym in ASSETS]
    # build reverse map: "kline.15.BTCUSDT" -> "BTC/USDT"
    topic_map = {f"kline.{TF_MINS}.{sym.replace('/', '')}": sym for sym in ASSETS}

    logger.info(f"Connecting to Bybit WebSocket — topics: {topics}")
    while True:
        try:
            async with websockets.connect(ws_url, ping_interval=20, ping_timeout=10) as ws:
                await ws.send(json.dumps({"op": "subscribe", "args": topics}))
                logger.success("Subscribed to all asset streams.")
                async for message in ws:
                    data = json.loads(message)
                    topic = data.get("topic", "")
                    if topic in topic_map and "data" in data:
                        k = data["data"][0]
                        sym = topic_map[topic]
                        engines[sym].update_candle(
                            int(k["start"]) // 1000,
                            float(k["open"]), float(k["high"]),
                            float(k["low"]),  float(k["close"]),
                            float(k["volume"]), k["confirm"], loop,
                        )
        except Exception as e:
            logger.error(f"WebSocket error: {e}. Reconnecting in 5s…")
            await asyncio.sleep(5)

def run_ws_loop():
    global loop
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(poll_bybit_websocket())

# ── Startup ───────────────────────────────────────────────────────────────────
@app.on_event("startup")
def startup_event():
    for eng in engines.values():
        eng.load_models()
        eng.fetch_initial_candles()
    threading.Thread(target=run_ws_loop, daemon=True).start()
    logger.success("All engines started — Predictor v2 online.")

# ── REST endpoints ────────────────────────────────────────────────────────────
@app.get("/api/status")
def get_status(symbol: Optional[str] = Query(default=None)):
    if symbol and symbol in engines:
        snap = engines[symbol].snapshot()
        return {
            "status": "online", "exchange": config["exchange"],
            "symbol": symbol, "timeframe": TIMEFRAME,
            "latest_prediction":       snap["latest_prediction_long"],
            "latest_prediction_long":  snap["latest_prediction_long"],
            "latest_prediction_short": snap["latest_prediction_short"],
            "latest_signal": snap["latest_signal"],
            "position": snap["position"],
            "stats": snap["stats"],
        }
    # Return summary for all assets
    return {
        "status": "online",
        "exchange": config["exchange"],
        "timeframe": TIMEFRAME,
        "assets": {
            sym: {
                "latest_prediction_long":  eng.latest_prediction_long,
                "latest_prediction_short": eng.latest_prediction_short,
                "latest_signal": eng.latest_signal,
                "position": eng.position,
                "stats": eng._stats(),
            } for sym, eng in engines.items()
        }
    }

@app.get("/api/candles")
def get_candles(symbol: Optional[str] = Query(default="BTC/USDT")):
    sym = symbol if symbol in engines else "BTC/USDT"
    return engines[sym].snapshot()["candles"]

@app.get("/api/trades")
def get_trades(symbol: Optional[str] = Query(default=None)):
    if symbol and symbol in engines:
        return engines[symbol].snapshot()["trades"]
    # All trades combined, sorted by exit_time descending
    all_trades = []
    for eng in engines.values():
        all_trades.extend(eng.trades_history)
    return sorted(all_trades, key=lambda t: t.get("exit_time", 0), reverse=True)

@app.get("/api/assets")
def get_assets():
    return {"assets": ASSETS}

# ── Enriched signal endpoints (Hermes signal agent ↔ dashboard) ───────────────

@app.post("/api/enriched-signal/{asset}")
async def post_enriched_signal(asset: str, payload: dict):
    """
    Written by the Hermes signal agent when a high-confidence event fires.
    `asset` is URL-encoded, e.g. 'BTC%2FUSDT' or 'BTC_USDT'.
    The agent should normalise to 'BTC/USDT' form before POSTing.
    """
    # Accept both BTC_USDT and BTC/USDT spellings from callers
    sym = asset.replace("_", "/")
    if sym not in engines and asset not in engines:
        raise HTTPException(status_code=404, detail=f"Unknown asset: {asset}")
    sym = sym if sym in engines else asset

    payload["received_at"] = datetime.now(timezone.utc).isoformat()
    _enriched_signals[sym] = payload
    logger.info(f"[signal-agent] Enriched signal received for {sym}: {payload.get('signal')} | {payload.get('confidence')}")

    # Broadcast to all dashboard WS clients
    if loop and loop.is_running():
        asyncio.run_coroutine_threadsafe(
            manager.broadcast({"type": "enriched_signal", "asset": sym, "signal": payload}),
            loop,
        )

    return {"status": "ok", "asset": sym}


@app.get("/api/enriched-signal/{asset}")
def get_enriched_signal(asset: str):
    """
    Read by the dashboard to display the latest Hermes-enriched signal.
    Returns 204 No Content if no signal has been posted yet.
    """
    sym = asset.replace("_", "/")
    sym = sym if sym in _enriched_signals else asset
    sig = _enriched_signals.get(sym)
    if sig is None:
        return JSONResponse(status_code=204, content=None)
    return sig


@app.get("/api/enriched-signals")
def get_all_enriched_signals():
    """All enriched signals keyed by asset."""
    return _enriched_signals


# ── Hermes Chat endpoint ──────────────────────────────────────────────────────

class _ChatMsg(BaseModel):
    role: str   # "user" | "assistant"
    content: str

class _ChatRequest(BaseModel):
    message: str
    symbol: str = "BTC/USDT"
    history: List[_ChatMsg] = []


@app.post("/api/chat")
async def hermes_chat(req: _ChatRequest):
    """
    Hermes signal-agent interactive chat.
    Returns a context-aware reply drawn from the live signal state.
    If OLLAMA_URL is set, forwards to Ollama; otherwise falls back to
    scripted signal-aware responses so the UI always works.
    """
    sym = req.symbol if req.symbol in engines else "BTC/USDT"
    eng = engines[sym]

    with eng.lock:
        signal     = eng.latest_signal
        long_prob  = eng.latest_prediction_long
        short_prob = eng.latest_prediction_short
        pos        = eng.position
        stats      = eng._stats()

    enriched   = _enriched_signals.get(sym, {})
    pos_str    = (
        f"{pos['type']} @ {pos['entry_price']:.2f} | TP {pos['tp']:.2f} | SL {pos['sl']:.2f}"
        if pos else "flat"
    )

    system_ctx = (
        f"You are Hermes, the signal intelligence agent for the Antigravity Predictor system. "
        f"Current state for {sym}: signal={signal}, long_prob={long_prob:.4f}, "
        f"short_prob={short_prob:.4f}, position={pos_str}, "
        f"total_trades={stats['total_trades']}, win_trades={stats['win_trades']}, "
        f"net_pnl={stats['total_pnl']:.2f} USDT. "
        f"Enriched context: {enriched.get('analyst_note', 'none')}. "
        f"Provide concise advisory signal commentary. "
        f"You do NOT execute trades. Keep responses under 3 sentences."
    )

    # ── Try Ollama first ──────────────────────────────────────────────
    ollama_url = os.environ.get("OLLAMA_URL", "").rstrip("/")
    if ollama_url:
        try:
            messages = [{"role": "system", "content": system_ctx}]
            for h in req.history[-8:]:
                messages.append({"role": h.role, "content": h.content})
            messages.append({"role": "user", "content": req.message})
            resp = requests.post(
                f"{ollama_url}/api/chat",
                json={
                    "model": os.environ.get("OLLAMA_MODEL", "llama3.2"),
                    "messages": messages,
                    "stream": False,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                reply = resp.json().get("message", {}).get("content", "").strip()
                if reply:
                    return {
                        "reply": reply,
                        "source": "ollama",
                        "signal": signal,
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
        except Exception as e:
            logger.warning(f"[hermes-chat] Ollama unavailable: {e}")

    # ── Scripted fallback (always works, signal-aware) ────────────────
    msg = req.message.lower()
    sym_display = sym.split("/")[0]

    if any(w in msg for w in ["help", "what can", "capabilities"]):
        reply = (
            "I can report signal state, position status, session PnL, and enriched market context "
            f"for BTC, ETH, and SOL. Try: 'What is the {sym_display} signal?' or 'Show my stats'."
        )
    elif any(w in msg for w in ["position", "trade", "open", "entry", "flat"]):
        reply = f"[{sym}] Position: {pos_str}."
        if pos:
            held = pos.get("candles_held", 0)
            reply += f" Candles held: {held}."
    elif any(w in msg for w in ["stat", "pnl", "profit", "performance", "win"]):
        wr = 100.0 * stats["win_trades"] / stats["total_trades"] if stats["total_trades"] else 0.0
        reply = (
            f"[{sym}] Session: {stats['total_trades']} trades | "
            f"Win rate {wr:.1f}% | Net PnL {stats['total_pnl']:+.2f} USDT."
        )
    elif any(w in msg for w in ["enrich", "news", "context", "analyst", "risk"]):
        note = (
            enriched.get("analyst_note")
            or enriched.get("news_summary")
            or enriched.get("model_context")
        )
        if note:
            reply = f"[{sym}] Enriched context: {note}"
        else:
            reply = f"[{sym}] No enriched context available yet. Signal agent has not posted for this asset."
    else:
        # Default: report current signal with probabilities
        sig_text = {
            "BUY":     f"BUY signal — long probability {long_prob:.3f}. Advisory only.",
            "SELL":    f"SELL signal — short probability {short_prob:.3f}. Advisory only.",
            "EXIT":    f"EXIT signal — model suggests closing the active position.",
            "NEUTRAL": f"NEUTRAL — L={long_prob:.3f} | S={short_prob:.3f}. No high-confidence setup.",
        }.get(signal, f"Signal: {signal}.")
        reply = f"[{sym}] {sig_text}"

    return {
        "reply": reply,
        "source": "scripted",
        "signal": signal,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ── WebSocket endpoint ────────────────────────────────────────────────────────
@app.websocket("/ws")
async def ws_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        # Send init payload for active symbol (default BTC)
        snaps = {sym: eng.snapshot() for sym, eng in engines.items()}
        await websocket.send_json({
            "type": "init",
            "assets": ASSETS,
            "snapshots": {
                sym: {
                    "candles":             snap["candles"],
                    "prediction_long":     snap["latest_prediction_long"],
                    "prediction_short":    snap["latest_prediction_short"],
                    "signal":              snap["latest_signal"],
                    "position":            snap["position"],
                    "stats":               snap["stats"],
                } for sym, snap in snaps.items()
            }
        })
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception as e:
        logger.error(f"WS client error: {e}")
        manager.disconnect(websocket)

# ── Serve Dashboard ───────────────────────────────────────────────────────────
parent_dir = os.path.dirname(os.path.abspath(__file__))
dashboard_path = os.path.join(parent_dir, "dashboard")
if not os.path.exists(dashboard_path):
    dashboard_path = os.path.join(os.path.dirname(parent_dir), "dashboard")

if os.path.exists(dashboard_path):
    app.mount("/", StaticFiles(directory=dashboard_path, html=True), name="dashboard")
    logger.success(f"Mounted dashboard static files from {dashboard_path}")
else:
    logger.warning("Dashboard directory not found. Static files not mounted.")


if __name__ == "__main__":
    import uvicorn
    host = config["server"]["host"]
    port = config["server"]["port"]
    logger.info(f"Starting Predictor v2 on {host}:{port}")
    uvicorn.run(app, host=host, port=port)
