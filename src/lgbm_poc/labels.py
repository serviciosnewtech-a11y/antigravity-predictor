from __future__ import annotations

import pandas as pd
import numpy as np

from lgbm_poc.baseline import BASELINE


def label_tp_before_sl_1h(
    df: pd.DataFrame,
    horizon_bars: int = BASELINE.horizon_bars,
    tp_atr_mult: float = 1.5,
    sl_atr_mult: float = 1.0,
    round_trip_pct: float = 0.0015,  # 0.15% fee + slippage cushion
    tp_pct: float | None = None,
    sl_pct: float | None = None,
) -> pd.DataFrame:
    """
    Create a binary label:
    1 if take profit is reached before stop loss within the next horizon_bars candles.
    0 otherwise.
    Supports both fixed percentage targets and volatility-adaptive ATR targets.
    """
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    labels = []

    highs = out["high"].to_numpy()
    lows = out["low"].to_numpy()
    closes = out["close"].to_numpy()
    atrs = out["atr_proxy"].to_numpy()

    # Fill any zero ATR values with rolling average or default percentage-based volatility
    median_atr = np.nanmedian(atrs[atrs > 0]) if np.any(atrs > 0) else 0.001 * closes.mean()

    n = len(out)
    for i in range(n):
        entry = closes[i]
        atr = atrs[i] if atrs[i] > 0 else median_atr

        # Adjust target boundaries to account for round-trip transaction costs (slippage + fees)
        fee_drag = entry * round_trip_pct
        if tp_pct is not None and sl_pct is not None:
            tp = entry * (1.0 + tp_pct) + fee_drag
            sl = entry * (1.0 - sl_pct) + fee_drag
        else:
            tp = entry + (tp_atr_mult * atr) + fee_drag
            sl = entry - (sl_atr_mult * atr) + fee_drag

        if i + horizon_bars >= n:
            labels.append(pd.NA)
            continue

        hit = 0
        for j in range(i + 1, min(i + horizon_bars + 1, n)):
            high = highs[j]
            low = lows[j]

            # Double touch: if both hit in the same bar, assume stop loss was hit first
            if low <= sl and high >= tp:
                hit = 0
                break
            if high >= tp:
                hit = 1
                break
            if low <= sl:
                hit = 0
                break
        labels.append(hit)

    out["label_tp_before_sl_1h"] = labels
    return out


def label_short_tp_before_sl_1h(
    df: pd.DataFrame,
    horizon_bars: int = BASELINE.horizon_bars,
    tp_atr_mult: float = 1.5,
    sl_atr_mult: float = 1.0,
    round_trip_pct: float = 0.0015,  # 0.15% fee + slippage cushion
    tp_pct: float | None = None,
    sl_pct: float | None = None,
) -> pd.DataFrame:
    """
    Create a short-side binary label:
    1 if take profit is reached before stop loss within the next horizon_bars candles.
    0 otherwise.
    Supports both fixed percentage targets and volatility-adaptive ATR targets.
    """
    out = df.copy().sort_values("timestamp").reset_index(drop=True)
    labels = []

    highs = out["high"].to_numpy()
    lows = out["low"].to_numpy()
    closes = out["close"].to_numpy()
    atrs = out["atr_proxy"].to_numpy()

    # Fill any zero ATR values with rolling average or default percentage-based volatility
    median_atr = np.nanmedian(atrs[atrs > 0]) if np.any(atrs > 0) else 0.001 * closes.mean()

    n = len(out)
    for i in range(n):
        entry = closes[i]
        atr = atrs[i] if atrs[i] > 0 else median_atr

        # Adjust target boundaries to account for round-trip transaction costs (slippage + fees)
        fee_drag = entry * round_trip_pct
        if tp_pct is not None and sl_pct is not None:
            tp = entry * (1.0 - tp_pct) - fee_drag
            sl = entry * (1.0 + sl_pct) - fee_drag
        else:
            tp = entry - (tp_atr_mult * atr) - fee_drag
            sl = entry + (sl_atr_mult * atr) - fee_drag

        if i + horizon_bars >= n:
            labels.append(pd.NA)
            continue

        hit = 0
        for j in range(i + 1, min(i + horizon_bars + 1, n)):
            high = highs[j]
            low = lows[j]

            # Double touch: if both hit in the same bar, assume stop loss was hit first
            if high >= sl and low <= tp:
                hit = 0
                break
            if low <= tp:
                hit = 1
                break
            if high >= sl:
                hit = 0
                break
        labels.append(hit)

    out["label_short_tp_before_sl_1h"] = labels
    return out
