from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_ohlcv(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if p.suffix == ".feather":
        df = pd.read_feather(p)
    else:
        df = pd.read_parquet(p)
    if "date" in df.columns and "timestamp" not in df.columns:
        df = df.rename(columns={"date": "timestamp"})
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    return df.sort_values("timestamp").reset_index(drop=True)


def resample_ohlcv(df: pd.DataFrame, rule: str = "15min") -> pd.DataFrame:
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
