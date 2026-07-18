#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd


def _load_ohlcv(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if p.suffix == ".feather":
        df = pd.read_feather(p)
    else:
        df = pd.read_parquet(p)
    if "date" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"date": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    return df.sort_values("timestamp").reset_index(drop=True)


def resample_ohlcv(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    out = (
        df.set_index("timestamp")
        .resample(rule)
        .agg(
            open=("open", "first"),
            high=("high", "max"),
            low=("low", "min"),
            close=("close", "last"),
            volume=("volume", "sum"),
        )
        .dropna()
        .reset_index()
    )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Resample OHLCV cache to a higher timeframe.")
    parser.add_argument("--input", required=True, help="Input feather/parquet path.")
    parser.add_argument("--output", required=True, help="Output parquet path.")
    parser.add_argument("--rule", default="15min", help="Pandas resample rule, e.g. 15min.")
    args = parser.parse_args()

    raw = _load_ohlcv(args.input)
    out_df = resample_ohlcv(raw, args.rule)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_df.to_parquet(out_path, index=False)
    print(f"wrote {len(out_df)} rows to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
