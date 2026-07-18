#!/usr/bin/env python3
"""
fetch_macro.py — Download macro reference OHLCV from Yahoo Finance.

Assets: Gold (GC=F), Oil/WTI (CL=F), DXY (DX-Y.NYB), S&P500 (^GSPC), VIX (^VIX)

Saves daily OHLCV as parquet to data/macro/<asset>.parquet
Run on a schedule (hourly cron or systemd timer) to keep data fresh.

Usage:
    python3 fetch_macro.py [--data-dir data/macro] [--days 730]
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd


MACRO_TICKERS: dict[str, str] = {
    "gold": "GC=F",       # Gold Futures
    "oil":  "CL=F",       # WTI Crude Oil Futures
    "dxy":  "DX-Y.NYB",   # US Dollar Index
    "spx":  "^GSPC",      # S&P 500
    "vix":  "^VIX",       # CBOE Volatility Index
}


def _check_yfinance() -> None:
    try:
        import yfinance  # noqa: F401
    except ImportError:
        print("ERROR: yfinance not installed. Run: pip install yfinance", file=sys.stderr)
        sys.exit(1)


def fetch_daily_ohlcv(
    ticker: str,
    start: str,
    end: str,
) -> pd.DataFrame:
    import yfinance as yf
    raw = yf.download(
        ticker,
        start=start,
        end=end,
        interval="1d",
        progress=False,
        auto_adjust=True,
    )
    if raw.empty:
        raise ValueError(f"No data returned for ticker {ticker!r}")

    raw = raw.reset_index()
    # Flatten MultiIndex columns if present (yfinance sometimes returns them)
    if isinstance(raw.columns, pd.MultiIndex):
        raw.columns = [c[0].lower() if c[1] == "" else c[0].lower() for c in raw.columns]
    else:
        raw.columns = [str(c).lower() for c in raw.columns]

    # Normalise date column name
    date_col = next((c for c in raw.columns if "date" in c), raw.columns[0])
    raw = raw.rename(columns={date_col: "timestamp"})
    raw["timestamp"] = pd.to_datetime(raw["timestamp"], utc=True, errors="coerce")

    keep = [c for c in ["timestamp", "open", "high", "low", "close", "volume"] if c in raw.columns]
    df = raw[keep].dropna(subset=["timestamp", "close"]).copy()
    df = df.sort_values("timestamp").reset_index(drop=True)
    return df


def _add_macro_features(df: pd.DataFrame, name: str) -> pd.DataFrame:
    """Compute derived features used during model training (stationary, no lookahead)."""
    out = df.copy()
    close = out["close"].replace(0, pd.NA)

    out[f"{name}_return_1d"] = np.log(out["close"] / out["close"].shift(1).replace(0, pd.NA)).fillna(0.0)
    out[f"{name}_return_5d"] = np.log(out["close"] / out["close"].shift(5).replace(0, pd.NA)).fillna(0.0)

    ema_fast = out["close"].ewm(span=9, adjust=False).mean()
    ema_slow = out["close"].ewm(span=21, adjust=False).mean()
    out[f"{name}_ema_fast"]   = (ema_fast / close).fillna(1.0)       # normalised
    out[f"{name}_ema_slow"]   = (ema_slow / close).fillna(1.0)
    out[f"{name}_trend"]      = ((ema_fast - ema_slow) / close).fillna(0.0)
    out[f"{name}_trend_dir"]  = (
        (ema_fast > ema_slow).astype(int) - (ema_fast < ema_slow).astype(int)
    )
    return out


def save_parquet(df: pd.DataFrame, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_parquet(path, index=False)


def main() -> int:
    _check_yfinance()

    parser = argparse.ArgumentParser(description="Download macro reference data from Yahoo Finance.")
    parser.add_argument("--data-dir", default="data/macro", help="Output directory for parquet files.")
    parser.add_argument("--days", type=int, default=730, help="How many calendar days of history to fetch.")
    parser.add_argument("--assets", nargs="*", default=list(MACRO_TICKERS.keys()),
                        help="Which assets to fetch (default: all).")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    end   = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    start = (datetime.now(timezone.utc) - timedelta(days=args.days)).strftime("%Y-%m-%d")

    ok, failed = [], []
    for name in args.assets:
        ticker = MACRO_TICKERS.get(name)
        if not ticker:
            print(f"[WARN] Unknown asset {name!r}, skipping.")
            continue

        print(f"[{name.upper():>4}] Fetching {ticker} from {start} to {end}…", end=" ", flush=True)
        try:
            df = fetch_daily_ohlcv(ticker, start=start, end=end)
            df = _add_macro_features(df, name)
            out_path = data_dir / f"{name}.parquet"
            save_parquet(df, out_path)
            print(f"{len(df)} rows → {out_path}")
            ok.append(name)
        except Exception as exc:
            print(f"FAILED: {exc}")
            failed.append(name)

    print(f"\nDone: {len(ok)} ok, {len(failed)} failed.")
    if failed:
        print(f"Failed assets: {failed}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
