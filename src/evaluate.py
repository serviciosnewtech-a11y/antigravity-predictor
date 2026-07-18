from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
import json

import pandas as pd
from sklearn.metrics import roc_auc_score, log_loss

from .dataset import select_xy


def evaluate_model(model, test_df: pd.DataFrame, label_col: str = "label_tp_before_sl_1h") -> dict:
    X_test, y_test = select_xy(test_df, label_col=label_col)
    pred = model.predict_proba(X_test)[:, 1]
    out = {
        "test_logloss": log_loss(y_test, pred, labels=[0, 1]),
        "test_auc": roc_auc_score(y_test, pred) if y_test.nunique() > 1 else None,
        "rows": int(len(test_df)),
        "positives": int(y_test.sum()),
    }
    return out


def save_metrics(metrics: dict, out_path: str | Path) -> Path:
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(metrics, indent=2, sort_keys=True))
    return path

