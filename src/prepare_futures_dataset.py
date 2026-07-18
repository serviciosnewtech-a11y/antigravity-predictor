#!/usr/bin/env python3
from __future__ import annotations

import argparse

import _bootstrap  # noqa: F401
from lgbm_poc.features import build_feature_table
from lgbm_poc.futures import build_futures_market_frame, load_timeframe_frame
from lgbm_poc.io import write_parquet
from lgbm_poc.labels import label_tp_before_sl_1h


def main() -> int:
    parser = argparse.ArgumentParser(description="Build a futures-aware feature+label dataset.")
    parser.add_argument("--candles", required=True, help="Futures OHLCV file.")
    parser.add_argument("--mark", default=None, help="Optional 1h mark file.")
    parser.add_argument("--funding-rate", dest="funding_rate", default=None, help="Optional 1h funding-rate file.")
    parser.add_argument("--output", required=True, help="Output parquet dataset.")
    parser.add_argument("--resample-rule", default="15min")
    parser.add_argument("--horizon-bars", type=int, default=4)
    parser.add_argument("--tp-pct", type=float, default=0.003)
    parser.add_argument("--sl-pct", type=float, default=0.002)
    args = parser.parse_args()

    candles = load_timeframe_frame(args.candles)
    mark = load_timeframe_frame(args.mark) if args.mark else None
    funding = load_timeframe_frame(args.funding_rate) if args.funding_rate else None
    market = build_futures_market_frame(candles, mark=mark, funding=funding, resample_rule=args.resample_rule)
    feats = build_feature_table(market)
    labeled = label_tp_before_sl_1h(feats, horizon_bars=args.horizon_bars, tp_pct=args.tp_pct, sl_pct=args.sl_pct)
    labeled = labeled.dropna(subset=["label_tp_before_sl_1h"]).reset_index(drop=True)
    write_parquet(labeled, args.output)
    print(f"wrote {len(labeled)} rows to {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
