#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import _bootstrap  # noqa: F401
from lgbm_poc.evaluate import evaluate_model, save_metrics
from lgbm_poc.io import read_parquet
from lgbm_poc.train import TrainConfig, train_binary_model


def walk_forward_slices(df: pd.DataFrame, train_size: int = 2000, valid_size: int = 500, step: int = 500):
    start = 0
    while start + train_size + valid_size <= len(df):
        train_df = df.iloc[start : start + train_size]
        valid_df = df.iloc[start + train_size : start + train_size + valid_size]
        yield start, train_df, valid_df
        start += step


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a simple walk-forward evaluation.")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--train-size", type=int, default=200, help="Rows per training window.")
    parser.add_argument("--valid-size", type=int, default=50, help="Rows per validation window.")
    parser.add_argument("--step", type=int, default=50, help="Rows to advance each slice.")
    args = parser.parse_args()

    df = read_parquet(args.dataset).sort_values("timestamp").reset_index(drop=True)
    rows = []
    for idx, train_df, valid_df in walk_forward_slices(df, train_size=args.train_size, valid_size=args.valid_size, step=args.step):
        model, valid_metrics = train_binary_model(train_df, valid_df, TrainConfig())
        rows.append({"slice": idx, **valid_metrics})

    out = pd.DataFrame(rows)
    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out.to_csv(out_path, index=False)
    print(f"wrote {len(out)} walk-forward rows to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
