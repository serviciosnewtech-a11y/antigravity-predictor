from __future__ import annotations

from pathlib import Path
import pandas as pd


def ensure_parent(path: Path) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)


def read_parquet(path) -> pd.DataFrame:
    return pd.read_parquet(Path(path))


def write_parquet(df: pd.DataFrame, path) -> None:
    target = Path(path)
    ensure_parent(target)
    df.to_parquet(target, index=False)
