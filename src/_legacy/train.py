from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import json

import lightgbm as lgb
import pandas as pd
from sklearn.metrics import roc_auc_score, log_loss

try:
    from .dataset import select_xy
    from .io import ensure_parent
except ImportError:
    from dataset import select_xy
    from io import ensure_parent


@dataclass
class TrainConfig:
    num_leaves: int = 31
    learning_rate: float = 0.05
    n_estimators: int = 500
    max_depth: int = -1
    min_child_samples: int = 50
    subsample: float = 0.9
    colsample_bytree: float = 0.9
    scale_pos_weight: float = 1.0
    random_state: int = 42


def train_binary_model(
    train_df: pd.DataFrame,
    valid_df: pd.DataFrame,
    config: TrainConfig | None = None,
    label_col: str = "label_tp_before_sl_1h",
) -> tuple[lgb.LGBMClassifier, dict]:
    cfg = config or TrainConfig()
    X_train, y_train = select_xy(train_df, label_col=label_col)
    X_valid, y_valid = select_xy(valid_df, label_col=label_col)
    if cfg.scale_pos_weight is None:
        positives = float(y_train.sum())
        negatives = float(len(y_train) - positives)
        scale_pos_weight = negatives / positives if positives > 0 else 1.0
    else:
        scale_pos_weight = cfg.scale_pos_weight

    model = lgb.LGBMClassifier(
        objective="binary",
        num_leaves=cfg.num_leaves,
        learning_rate=cfg.learning_rate,
        n_estimators=cfg.n_estimators,
        max_depth=cfg.max_depth,
        min_child_samples=cfg.min_child_samples,
        subsample=cfg.subsample,
        colsample_bytree=cfg.colsample_bytree,
        scale_pos_weight=scale_pos_weight,
        random_state=cfg.random_state,
        n_jobs=-1,
    )

    model.fit(
        X_train,
        y_train,
        eval_set=[(X_valid, y_valid)],
        eval_metric="binary_logloss",
        callbacks=[lgb.early_stopping(30, verbose=False)],
    )

    valid_pred = model.predict_proba(X_valid)[:, 1]
    metrics = {
        "valid_auc": roc_auc_score(y_valid, valid_pred) if y_valid.nunique() > 1 else None,
        "valid_logloss": log_loss(y_valid, valid_pred, labels=[0, 1]),
        "best_iteration": int(getattr(model, "best_iteration_", 0) or 0),
        "scale_pos_weight": scale_pos_weight,
    }
    return model, metrics


def save_model(model: lgb.LGBMClassifier, out_dir: str | Path, metadata: dict) -> Path:
    out_path = Path(out_dir)
    out_path.mkdir(parents=True, exist_ok=True)
    model_path = out_path / "model.txt"
    meta_path = out_path / "metadata.json"
    model.booster_.save_model(str(model_path))
    ensure_parent(meta_path)
    meta_path.write_text(json.dumps(metadata, indent=2, sort_keys=True))
    return model_path
