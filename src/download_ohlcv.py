#!/usr/bin/env python3
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import _bootstrap  # noqa: F401
import ccxt
import pandas as pd

from lgbm_poc.baseline import BASELINE


def _parse_iso_to_ms(value: str | None) -> int | None:
    if value is None:
        return None
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def _make_exchange(exchange_id: str, market_type: str) -> ccxt.Exchange:
    exchange_cls = getattr(ccxt, exchange_id, None)
    if exchange_cls is None:
        raise ValueError(f"unsupported exchange id: {exchange_id}")
    exchange = exchange_cls({"enableRateLimit": True})
    if market_type:
        exchange.options["defaultType"] = market_type
    return exchange


def fetch_ohlcv(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    since_ms: int | None,
    limit: int,
    market_type: str,
    max_rows: int | None = None,
) -> pd.DataFrame:
    # Special market types use dedicated ccxt endpoints
    if market_type == "mark_price":
        return _fetch_mark_ohlcv(exchange_id, symbol, timeframe, since_ms, limit, max_rows)
    if market_type == "funding_rate":
        return _fetch_funding_history(exchange_id, symbol, since_ms, limit, max_rows)

    exchange = _make_exchange(exchange_id, market_type)
    rows: list[list[float]] = []
    cursor = since_ms

    while True:
        batch = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit)
        if not batch:
            break
        if rows and batch[0][0] <= rows[-1][0]:
            batch = [row for row in batch if row[0] > rows[-1][0]]
        if not batch:
            break
        rows.extend(batch)
        if max_rows is not None and len(rows) >= max_rows:
            rows = rows[:max_rows]
            break
        cursor = batch[-1][0] + 1

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def _fetch_mark_ohlcv(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    since_ms: int | None,
    limit: int,
    max_rows: int | None,
) -> pd.DataFrame:
    exchange = _make_exchange(exchange_id, "swap")
    rows: list[list[float]] = []
    cursor = since_ms

    while True:
        batch = exchange.fetch_mark_ohlcv(symbol, timeframe=timeframe, since=cursor, limit=limit)
        if not batch:
            break
        if rows and batch[0][0] <= rows[-1][0]:
            batch = [row for row in batch if row[0] > rows[-1][0]]
        if not batch:
            break
        rows.extend(batch)
        if max_rows is not None and len(rows) >= max_rows:
            rows = rows[:max_rows]
            break
        cursor = batch[-1][0] + 1

    df = pd.DataFrame(rows, columns=["timestamp", "open", "high", "low", "close", "volume"])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def _fetch_funding_history(
    exchange_id: str,
    symbol: str,
    since_ms: int | None,
    limit: int,
    max_rows: int | None,
) -> pd.DataFrame:
    exchange = _make_exchange(exchange_id, "swap")
    rows: list[dict] = []
    cursor = since_ms

    while True:
        batch = exchange.fetch_funding_rate_history(symbol, since=cursor, limit=limit)
        if not batch:
            break
        if rows and batch[0]["timestamp"] <= rows[-1]["timestamp"]:
            batch = [r for r in batch if r["timestamp"] > rows[-1]["timestamp"]]
        if not batch:
            break
        rows.extend(batch)
        if max_rows is not None and len(rows) >= max_rows:
            rows = rows[:max_rows]
            break
        cursor = batch[-1]["timestamp"] + 1

    if not rows:
        return pd.DataFrame(columns=["timestamp", "funding_rate"])

    df = pd.DataFrame([{"timestamp": r["timestamp"], "funding_rate": r.get("fundingRate", 0.0)} for r in rows])
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", utc=True)
    return df


def main() -> int:
    parser = argparse.ArgumentParser(description="Exchange OHLCV loader for the BTC/USDT baseline.")
    parser.add_argument("--exchange-id", default="bybit", help="ccxt exchange id, e.g. bybit or binance")
    parser.add_argument("--symbol", default=BASELINE.symbol)
    parser.add_argument("--timeframe", default=BASELINE.timeframe)
    parser.add_argument("--since-ms", type=int, default=None)
    parser.add_argument("--since", default=None, help="ISO-8601 start time, e.g. 2025-01-01T00:00:00Z")
    parser.add_argument("--limit", type=int, default=BASELINE.limit)
    parser.add_argument("--max-rows", type=int, default=BASELINE.max_rows)
    parser.add_argument("--market-type", default=BASELINE.market_type,
                        choices=["spot", "swap", "mark_price", "funding_rate"])
    parser.add_argument("--output-parquet", required=True, help="Where to save parquet.")
    args = parser.parse_args()

    since_ms = args.since_ms if args.since_ms is not None else _parse_iso_to_ms(args.since)
    df = fetch_ohlcv(
        args.exchange_id,
        args.symbol,
        args.timeframe,
        since_ms,
        args.limit,
        args.market_type,
        args.max_rows,
    )
    out = Path(args.output_parquet)
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(out, index=False)
    print(f"wrote {len(df)} rows to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
