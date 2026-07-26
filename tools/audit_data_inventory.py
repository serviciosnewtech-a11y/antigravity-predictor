#!/usr/bin/env python3
"""
tools/audit_data_inventory.py — Comprehensive Data Inventory for .retrain_cache/

Inspects every parquet file in .retrain_cache/ and emits reports/data_inventory.json
with complete file metadata, row counts, timestamp ranges, column schemas, inferred
bar intervals, SHA256 checksums, tie-bar statistics, and base directional rates.
"""

import os
import glob
import json
import hashlib
from datetime import datetime, timezone
import pandas as pd
import numpy as np


def compute_sha256(filepath: str) -> str:
    h = hashlib.sha256()
    with open(filepath, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def format_interval(seconds: float) -> str:
    if seconds <= 0 or np.isnan(seconds):
        return "unknown"
    if abs(seconds - 60) < 5:
        return "1m"
    if abs(seconds - 300) < 15:
        return "5m"
    if abs(seconds - 900) < 30:
        return "15m"
    if abs(seconds - 3600) < 60:
        return "1h"
    if abs(seconds - 14400) < 120:
        return "4h"
    if abs(seconds - 86400) < 300:
        return "1d"
    return f"{seconds:.0f}s"


def main():
    repo_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    cache_dir = os.path.join(repo_root, ".retrain_cache")
    reports_dir = os.path.join(repo_root, "reports")
    os.makedirs(reports_dir, exist_ok=True)

    out_json = os.path.join(reports_dir, "data_inventory.json")

    parquet_files = sorted(glob.glob(os.path.join(cache_dir, "*.parquet")))
    if not parquet_files:
        raise FileNotFoundError(f"No parquet files found in {cache_dir}")

    inventory = {}

    for pfile in parquet_files:
        rel_path = os.path.relpath(pfile, repo_root)
        fname = os.path.basename(pfile)
        file_size = os.path.getsize(pfile)
        sha256 = compute_sha256(pfile)

        df = pd.read_parquet(pfile)
        row_count = len(df)
        cols = list(df.columns)

        start_iso, start_epoch = None, None
        end_iso, end_epoch = None, None
        modal_sec, modal_str = None, "unknown"

        # Look for timestamp column
        time_col = None
        for col_cand in ["timestamp", "time", "datetime", "date", "open_time"]:
            if col_cand in df.columns:
                time_col = col_cand
                break

        if time_col and row_count > 0:
            ts_series = df[time_col]
            # Convert to numeric epoch if datetime or string
            if pd.api.types.is_datetime64_any_dtype(ts_series):
                epoch_series = ts_series.astype("int64") // 10**9
            else:
                epoch_series = pd.to_numeric(ts_series, errors="coerce")

            valid_ts = epoch_series.dropna()
            if not valid_ts.empty:
                start_epoch = float(valid_ts.min())
                end_epoch = float(valid_ts.max())

                # Standardize timestamps > 1e11 (ms) to seconds
                if start_epoch > 1e11:
                    start_epoch /= 1000.0
                    end_epoch /= 1000.0
                    valid_ts = valid_ts / 1000.0

                start_iso = datetime.fromtimestamp(start_epoch, tz=timezone.utc).isoformat()
                end_iso = datetime.fromtimestamp(end_epoch, tz=timezone.utc).isoformat()

                if len(valid_ts) > 1:
                    diffs = valid_ts.sort_values().diff().dropna()
                    if not diffs.empty:
                        modal_sec = float(diffs.mode().iloc[0]) if not diffs.mode().empty else float(diffs.median())
                        modal_str = format_interval(modal_sec)

        # Check close price for ties and directional rate
        tie_bars_count = None
        base_rate_2bar = None
        close_col = None
        for c_cand in ["close", "close_price"]:
            if c_cand in df.columns:
                close_col = c_cand
                break

        if close_col and row_count >= 3:
            close = df[close_col].values
            c0 = close[:-2]
            c2 = close[2:]
            ties = np.sum(c2 == c0)
            long_pos = np.sum(c2 > c0)
            valid_pairs = len(c0)
            tie_bars_count = int(ties)
            if valid_pairs > 0:
                base_rate_2bar = float(long_pos / valid_pairs)

        inventory[fname] = {
            "rel_path": rel_path,
            "file_size_bytes": file_size,
            "sha256": sha256,
            "row_count": row_count,
            "column_count": len(cols),
            "columns": cols,
            "start_timestamp_epoch": start_epoch,
            "start_timestamp_iso": start_iso,
            "end_timestamp_epoch": end_epoch,
            "end_timestamp_iso": end_iso,
            "inferred_interval_seconds": modal_sec,
            "inferred_interval": modal_str,
            "tie_bars_2bar": tie_bars_count,
            "base_rate_direction_2bar": base_rate_2bar,
        }

    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "total_files": len(inventory),
        "inventory": inventory
    }

    with open(out_json, "w") as f:
        json.dump(report, f, indent=2)

    print(f"Data inventory complete. Generated {out_json} with {len(inventory)} files.")


if __name__ == "__main__":
    main()
