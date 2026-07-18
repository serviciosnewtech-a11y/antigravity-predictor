#!/usr/bin/env python3
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401
from lgbm_poc.baseline import BASELINE
from lgbm_poc.features import build_feature_table
from lgbm_poc.io import read_parquet, write_parquet
from lgbm_poc.labels import label_tp_before_sl_1h, label_short_tp_before_sl_1h


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a cached BTC/USDT feature+label dataset.")
    parser.add_argument("--input", required=True, help="Raw OHLCV parquet file.")
    parser.add_argument("--output", required=True, help="Output parquet dataset.")
    parser.add_argument("--horizon-bars", type=int, default=BASELINE.horizon_bars)
    parser.add_argument("--tp-atr-mult", type=float, default=1.5)
    parser.add_argument("--sl-atr-mult", type=float, default=1.0)
    parser.add_argument("--tp-pct", type=float, default=None)
    parser.add_argument("--sl-pct", type=float, default=None)
    args = parser.parse_args()

    raw = read_parquet(args.input)
    feats = build_feature_table(raw)
    
    # 1. Label long positions
    labeled = label_tp_before_sl_1h(
        feats, 
        horizon_bars=args.horizon_bars, 
        tp_atr_mult=args.tp_atr_mult, 
        sl_atr_mult=args.sl_atr_mult,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct
    )
    
    # 2. Label short positions
    labeled = label_short_tp_before_sl_1h(
        labeled, 
        horizon_bars=args.horizon_bars, 
        tp_atr_mult=args.tp_atr_mult, 
        sl_atr_mult=args.sl_atr_mult,
        tp_pct=args.tp_pct,
        sl_pct=args.sl_pct
    )
    
    # Rename target columns
    labeled = labeled.rename(columns={
        "label_tp_before_sl_1h": "label_long",
        "label_short_tp_before_sl_1h": "label_short"
    })
    
    labeled = labeled.dropna(subset=["label_long", "label_short"]).reset_index(drop=True)
    write_parquet(labeled, args.output)
    print(f"wrote {len(labeled)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
