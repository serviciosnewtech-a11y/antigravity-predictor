#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

import lightgbm as lgb
import pandas as pd

import _bootstrap  # noqa: F401
from lgbm_poc.dataset import FEATURE_COLUMNS, LABEL_COLUMN, select_xy
from lgbm_poc.io import read_parquet


def build_summary(dataset_path: str | Path, model_path: str | Path, metrics_path: str | Path | None) -> dict:
    df = read_parquet(dataset_path).sort_values("timestamp").reset_index(drop=True)
    X, y = select_xy(df)

    booster = lgb.Booster(model_file=str(model_path))
    feature_names = list(booster.feature_name())
    gain = booster.feature_importance(importance_type="gain")
    split = booster.feature_importance(importance_type="split")
    if len(feature_names) != len(gain):
        feature_names = list(X.columns[: len(gain)])
    importance = pd.DataFrame(
        {
            "feature": feature_names,
            "gain": gain,
            "split": split,
        }
    ).sort_values(["gain", "split"], ascending=False)

    metrics = {}
    if metrics_path:
        metrics = json.loads(Path(metrics_path).read_text())

    summary = {
        "dataset": str(dataset_path),
        "model": str(model_path),
        "rows": int(len(X)),
        "feature_rows": int(len(X)),
        "feature_count": int(len(X.columns)),
        "positives": int(y.sum()),
        "negatives": int((1 - y).sum()),
        "positive_rate": float(y.mean()) if len(y) else None,
        "metrics": metrics,
        "top_importance": importance.head(15).to_dict(orient="records"),
        "zero_gain_features": importance.loc[importance["gain"] == 0, "feature"].tolist(),
    }
    return summary


def render_markdown(summary: dict) -> str:
    lines = [
        "# Run Summary",
        "",
        f"- Dataset: `{summary['dataset']}`",
        f"- Model: `{summary['model']}`",
        f"- Rows: `{summary['rows']}`",
        f"- Feature rows: `{summary['feature_rows']}`",
        f"- Feature count: `{summary['feature_count']}`",
        f"- Positives: `{summary['positives']}`",
        f"- Negatives: `{summary['negatives']}`",
        f"- Positive rate: `{summary['positive_rate']:.4f}`" if summary["positive_rate"] is not None else "- Positive rate: n/a",
        "",
        "## Metrics",
    ]
    metrics = summary.get("metrics", {})
    if metrics:
        for key in sorted(metrics):
            lines.append(f"- {key}: `{metrics[key]}`")
    else:
        lines.append("- n/a")

    lines.extend(
        [
            "",
            "## Top Feature Importance",
        ]
    )
    for item in summary["top_importance"]:
        lines.append(f"- {item['feature']}: gain `{item['gain']:.6f}`, split `{int(item['split'])}`")

    lines.extend(
        [
            "",
            "## Zero-Gain Features",
        ]
    )
    if summary["zero_gain_features"]:
        for feature in summary["zero_gain_features"]:
            lines.append(f"- {feature}")
    else:
        lines.append("- none")

    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize a LightGBM run with feature importances.")
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--metrics", default=None)
    parser.add_argument("--output-md", required=True)
    parser.add_argument("--output-csv", required=True)
    args = parser.parse_args()

    summary = build_summary(args.dataset, args.model, args.metrics)
    out_md = Path(args.output_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(render_markdown(summary))

    df = pd.DataFrame(summary["top_importance"])
    out_csv = Path(args.output_csv)
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out_csv, index=False)

    print(f"wrote {out_md}")
    print(f"wrote {out_csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
