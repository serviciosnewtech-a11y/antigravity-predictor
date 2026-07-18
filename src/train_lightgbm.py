#!/usr/bin/env python3
from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

import _bootstrap  # noqa: F401
from lgbm_poc.baseline import baseline_dict
from lgbm_poc.evaluate import evaluate_model, save_metrics
from lgbm_poc.io import read_parquet
from lgbm_poc.train import TrainConfig, save_model, train_binary_model


def time_split(df: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    n = len(df)
    train_end = int(n * 0.7)
    valid_end = int(n * 0.85)
    return df.iloc[:train_end], df.iloc[train_end:valid_end], df.iloc[valid_end:]


def main() -> int:
    parser = argparse.ArgumentParser(description="Train a CPU-friendly LightGBM binary baseline.")
    parser.add_argument("--dataset", required=True, help="Cached parquet dataset.")
    parser.add_argument("--output-dir", required=True, help="Directory for model and metrics.")
    parser.add_argument("--label-col", default="label_tp_before_sl_1h", help="Target label column.")
    args = parser.parse_args()

    df = read_parquet(args.dataset).sort_values("timestamp").reset_index(drop=True)
    train_df, valid_df, test_df = time_split(df)

    model, valid_metrics = train_binary_model(train_df, valid_df, TrainConfig(), label_col=args.label_col)
    test_metrics = evaluate_model(model, test_df, label_col=args.label_col)

    out_dir = Path(args.output_dir)
    model_path = save_model(model, out_dir / "model", {
        "dataset": args.dataset,
        "label_col": args.label_col,
        "baseline": baseline_dict(),
        "train_rows": len(train_df),
        "valid_rows": len(valid_df),
        "test_rows": len(test_df),
        "valid_metrics": valid_metrics,
        "test_metrics": test_metrics,
    })
    save_metrics({**valid_metrics, **test_metrics}, out_dir / "metrics.json")
    print(f"saved model to {model_path}")
    print(f"test metrics: {test_metrics}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
