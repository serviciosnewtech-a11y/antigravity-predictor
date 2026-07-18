from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_timeframe_frame(path: str | Path) -> pd.DataFrame:
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


def _normalize_mark_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "date" in out.columns and "timestamp" not in out.columns:
        out = out.rename(columns={"date": "timestamp"})
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    out = out.sort_values("timestamp").reset_index(drop=True)
    out = out.rename(columns={"close": "mark_close"})
    if "mark_close" not in out.columns:
        out["mark_close"] = out["open"]
    return out[["timestamp", "mark_close"]]


def _normalize_funding_frame(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    if "date" in out.columns and "timestamp" not in out.columns:
        out = out.rename(columns={"date": "timestamp"})
    out["timestamp"] = pd.to_datetime(out["timestamp"], utc=True, errors="coerce")
    out = out.sort_values("timestamp").reset_index(drop=True)
    if "funding_rate" in out.columns:
        rates = out["funding_rate"]
    else:
        rates = out["open"]
    out["funding_rate"] = pd.to_numeric(rates, errors="coerce").fillna(0)
    return out[["timestamp", "funding_rate"]]


def build_futures_market_frame(
    candles: pd.DataFrame,
    mark: pd.DataFrame | None = None,
    funding: pd.DataFrame | None = None,
    resample_rule: str = "15min",
) -> pd.DataFrame:
    base = resample_ohlcv(candles, resample_rule)

    if mark is not None:
        mark_frame = _normalize_mark_frame(mark)
        base = pd.merge_asof(base.sort_values("timestamp"), mark_frame, on="timestamp", direction="backward")
    else:
        base["mark_close"] = base["close"]

    if funding is not None:
        funding_frame = _normalize_funding_frame(funding)
        base = pd.merge_asof(base.sort_values("timestamp"), funding_frame, on="timestamp", direction="backward")
    else:
        base["funding_rate"] = 0.0

    base["mark_close"] = base["mark_close"].fillna(base["close"])
    base["funding_rate"] = base["funding_rate"].fillna(0)
    return base.sort_values("timestamp").reset_index(drop=True)
