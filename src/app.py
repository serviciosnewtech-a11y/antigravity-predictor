from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq
import plotly.graph_objects as go
import streamlit as st


ROOT = Path(__file__).resolve().parents[1]
WORKSPACE_ROOT = ROOT.parent
PLAN_PACK = WORKSPACE_ROOT / "plan_pack" / "08_pending_tasks.md"
REPORTS_ROOT = ROOT / "reports"
DATA_ROOT = ROOT / "data"
BTC_5Y_DATA = DATA_ROOT / "binance_5y" / "BTC_USDT-15m.parquet"
BTC_5Y_DATASET = DATA_ROOT / "binance_5y" / "BTC_USDT-15m_dataset.parquet"
BTC_5Y_CROSS_DATASET = DATA_ROOT / "binance_5y" / "BTC_ETH_SOL_15m_dataset.parquet"


def _parse_summary_markdown(path: Path) -> dict:
    values: dict[str, str | float | int] = {}
    if not path.exists():
        return values
    line_re = re.compile(r"^- ([^:]+): `([^`]*)`$")
    for line in path.read_text().splitlines():
        match = line_re.match(line.strip())
        if not match:
            continue
        key, raw = match.groups()
        try:
            values[key] = int(raw)
        except ValueError:
            try:
                values[key] = float(raw)
            except ValueError:
                values[key] = raw
    return values


def _load_run(path: Path) -> dict:
    metrics_path = path / "metrics.json"
    summary_path = path / "summary.md"
    walkforward_path = path / "walkforward.csv"
    feature_importance_path = path / "feature_importance.csv"

    metrics = json.loads(metrics_path.read_text()) if metrics_path.exists() else {}
    summary = _parse_summary_markdown(summary_path)
    top_features = pd.read_csv(feature_importance_path) if feature_importance_path.exists() else None

    walk_mean_auc = None
    walk_mean_logloss = None
    if walkforward_path.exists():
        walk = pd.read_csv(walkforward_path)
        if not walk.empty:
            if "valid_auc" in walk.columns:
                walk_mean_auc = float(walk["valid_auc"].mean())
            if "valid_logloss" in walk.columns:
                walk_mean_logloss = float(walk["valid_logloss"].mean())

    return {
        "name": path.name,
        "path": path,
        "metrics": metrics,
        "summary": summary,
        "top_features": top_features,
        "walkforward_mean_valid_auc": walk_mean_auc,
        "walkforward_mean_valid_logloss": walk_mean_logloss,
    }


def discover_runs() -> list[dict]:
    runs = []
    if not REPORTS_ROOT.exists():
        return runs
    for metrics_path in sorted(REPORTS_ROOT.glob("*/metrics.json")):
        runs.append(_load_run(metrics_path.parent))
    runs.sort(key=lambda item: item["path"].stat().st_mtime, reverse=True)
    return runs


def _parquet_inventory(root: Path) -> pd.DataFrame:
    rows = []
    if not root.exists():
        return pd.DataFrame(rows)
    for path in sorted(root.rglob("*.parquet")):
        try:
            meta = pq.ParquetFile(path).metadata
            num_rows = meta.num_rows if meta is not None else None
            num_cols = meta.num_columns if meta is not None else None
        except Exception:
            num_rows = None
            num_cols = None
        rows.append(
            {
                "file": str(path.relative_to(ROOT)),
                "rows": num_rows,
                "cols": num_cols,
            }
        )
    return pd.DataFrame(rows)


def _load_tasks() -> tuple[dict[str, int], list[str], list[str]]:
    counts = {"pending": 0, "active": 0, "blocked": 0, "done": 0}
    active_lines: list[str] = []
    pending_lines: list[str] = []
    if not PLAN_PACK.exists():
        return counts, active_lines, pending_lines
    for raw in PLAN_PACK.read_text().splitlines():
        line = raw.strip()
        match = re.match(r"- `(?P<status>pending|active|blocked|done)` (?P<task>.+)", line)
        if not match:
            continue
        status = match.group("status")
        task = match.group("task")
        counts[status] += 1
        if status == "active":
            active_lines.append(task)
        elif status == "pending":
            pending_lines.append(task)
    return counts, active_lines, pending_lines


def _metric_or_none(run: dict, key: str) -> float | None:
    value = run["metrics"].get(key) or run["summary"].get(key)
    if value is None:
        return None
    try:
        return float(value)
    except Exception:
        return None


def _load_price_frame(path: Path, tail: int = 240) -> pd.DataFrame:
    if not path.exists():
        return pd.DataFrame()
    df = pd.read_parquet(path).sort_values("timestamp").tail(tail).reset_index(drop=True)
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    return df


def _candlestick_figure(df: pd.DataFrame) -> go.Figure:
    fig = go.Figure()
    if df.empty:
        fig.update_layout(
            template="plotly_dark",
            paper_bgcolor="#0b0f14",
            plot_bgcolor="#0b0f14",
            margin=dict(l=20, r=20, t=20, b=20),
        )
        return fig

    fig.add_trace(
        go.Candlestick(
            x=df["timestamp"],
            open=df["open"],
            high=df["high"],
            low=df["low"],
            close=df["close"],
            name="BTC/USDT",
            increasing_line_color="#00c853",
            decreasing_line_color="#ff5252",
        )
    )
    fig.add_trace(
        go.Bar(
            x=df["timestamp"],
            y=df["volume"],
            name="Volume",
            marker_color="rgba(123, 123, 123, 0.35)",
            yaxis="y2",
            opacity=0.45,
        )
    )
    fig.update_layout(
        template="plotly_dark",
        paper_bgcolor="#0b0f14",
        plot_bgcolor="#0b0f14",
        margin=dict(l=20, r=20, t=20, b=20),
        height=560,
        xaxis=dict(
            rangeslider=dict(visible=False),
            gridcolor="rgba(255,255,255,0.06)",
            zeroline=False,
        ),
        yaxis=dict(
            title="Price",
            gridcolor="rgba(255,255,255,0.06)",
            side="right",
        ),
        yaxis2=dict(
            overlaying="y",
            side="left",
            showgrid=False,
            title="Volume",
            range=[0, float(df["volume"].max()) * 4 if not df["volume"].empty else 1],
            showticklabels=False,
        ),
        showlegend=False,
    )
    return fig


def _load_style() -> None:
    st.markdown(
        """
        <style>
        .stApp {
            background: linear-gradient(180deg, #081018 0%, #0b0f14 100%);
            color: #e8eef5;
        }
        section[data-testid="stSidebar"] {
            background: #0f141b;
            border-right: 1px solid rgba(255,255,255,0.07);
        }
        .tv-banner {
            padding: 1rem 1.2rem;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 14px;
            background: linear-gradient(135deg, rgba(24,33,44,0.95), rgba(12,17,24,0.95));
            box-shadow: 0 12px 36px rgba(0,0,0,0.35);
            margin-bottom: 1rem;
        }
        .tv-banner h1 {
            margin: 0;
            font-size: 1.6rem;
            letter-spacing: 0.02em;
        }
        .tv-banner p {
            margin: 0.25rem 0 0 0;
            color: #9fb0c1;
        }
        .tv-card {
            padding: 0.8rem 0.95rem;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
            background: rgba(14,19,27,0.95);
            box-shadow: inset 0 0 0 1px rgba(255,255,255,0.02);
        }
        .tv-card-label {
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: #8da1b5;
            margin-bottom: 0.3rem;
        }
        .tv-card-value {
            font-size: 1.4rem;
            font-weight: 700;
            color: #f5f7fa;
        }
        .tv-card-sub {
            font-size: 0.8rem;
            color: #8da1b5;
            margin-top: 0.15rem;
        }
        .tv-section {
            padding: 1rem 1rem 0.3rem 1rem;
            border: 1px solid rgba(255,255,255,0.08);
            border-radius: 14px;
            background: rgba(10,14,20,0.92);
            margin-bottom: 1rem;
        }
        .tv-section h2, .tv-section h3 {
            color: #f5f7fa;
            margin-top: 0;
        }
        .stDataFrame, .stTable {
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 10px;
            overflow: hidden;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def main() -> None:
    st.set_page_config(page_title="Trading Bot Dashboard", layout="wide")
    _load_style()

    st.markdown(
        """
        <div class="tv-banner">
            <h1>Trading Bot Control Surface</h1>
            <p>TradingView-style read-only dashboard for data, runs, and project state.</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    runs = discover_runs()
    counts, active_tasks, pending_tasks = _load_tasks()
    datasets = _parquet_inventory(DATA_ROOT)
    btc_frame = _load_price_frame(BTC_5Y_DATA)

    if runs:
        latest = runs[0]
        latest_auc = _metric_or_none(latest, "test_auc")
        latest_valid_auc = _metric_or_none(latest, "valid_auc")
        latest_logloss = _metric_or_none(latest, "test_logloss")
    else:
        latest = None
        latest_auc = latest_valid_auc = latest_logloss = None

    c1, c2, c3, c4 = st.columns(4)
    c1.markdown(
        f'<div class="tv-card"><div class="tv-card-label">Runs</div><div class="tv-card-value">{len(runs)}</div><div class="tv-card-sub">saved model artifacts</div></div>',
        unsafe_allow_html=True,
    )
    c2.markdown(
        f'<div class="tv-card"><div class="tv-card-label">Datasets</div><div class="tv-card-value">{0 if datasets.empty else len(datasets)}</div><div class="tv-card-sub">parquet files indexed</div></div>',
        unsafe_allow_html=True,
    )
    c3.markdown(
        f'<div class="tv-card"><div class="tv-card-label">Active tasks</div><div class="tv-card-value">{counts["active"]}</div><div class="tv-card-sub">open work items</div></div>',
        unsafe_allow_html=True,
    )
    c4.markdown(
        f'<div class="tv-card"><div class="tv-card-label">Done tasks</div><div class="tv-card-value">{counts["done"]}</div><div class="tv-card-sub">closed work items</div></div>',
        unsafe_allow_html=True,
    )

    if latest is not None:
        st.markdown('<div class="tv-section">', unsafe_allow_html=True)
        st.subheader(f"Latest run: {latest['name']}")
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Test AUC", f"{latest_auc:.4f}" if latest_auc is not None else "n/a")
        m2.metric("Valid AUC", f"{latest_valid_auc:.4f}" if latest_valid_auc is not None else "n/a")
        m3.metric("Test logloss", f"{latest_logloss:.4f}" if latest_logloss is not None else "n/a")
        m4.metric("Walk-forward AUC", f"{latest['walkforward_mean_valid_auc']:.4f}" if latest["walkforward_mean_valid_auc"] is not None else "n/a")
        st.markdown("</div>", unsafe_allow_html=True)

    tab_overview, tab_runs, tab_data, tab_tasks, tab_future = st.tabs(
        ["Overview", "Runs", "Data", "Tasks", "Future Path"]
    )

    with tab_overview:
        left, right = st.columns([1.55, 0.85])
        with left:
            st.markdown('<div class="tv-section">', unsafe_allow_html=True)
            st.markdown("### BTC/USDT Market Panel")
            if btc_frame.empty:
                st.info("BTC 5-year price frame not found.")
            else:
                st.plotly_chart(_candlestick_figure(btc_frame), use_container_width=True, theme=None)
            st.markdown("</div>", unsafe_allow_html=True)

            st.markdown('<div class="tv-section">', unsafe_allow_html=True)
            st.markdown("### Baseline Snapshot")
            if latest is None:
                st.info("No runs found yet.")
            else:
                st.write(
                    {
                        "run": latest["name"],
                        "rows": latest["summary"].get("Rows"),
                        "feature_count": latest["summary"].get("Feature count"),
                        "positive_rate": latest["summary"].get("Positive rate"),
                        "walkforward_mean_valid_auc": latest["walkforward_mean_valid_auc"],
                        "walkforward_mean_valid_logloss": latest["walkforward_mean_valid_logloss"],
                    }
                )
                if latest["top_features"] is not None and not latest["top_features"].empty:
                    st.markdown("#### Top Feature Importance")
                    st.dataframe(latest["top_features"].head(10), use_container_width=True, hide_index=True)
            st.markdown("</div>", unsafe_allow_html=True)
        with right:
            st.markdown('<div class="tv-section">', unsafe_allow_html=True)
            st.markdown("### Project State")
            st.write(
                {
                    "BTC-only 5y baseline": "done",
                    "BTC + ETH + SOL context": "done",
                    "NotebookLM preprocessing": "pending",
                    "Dashboard": "active",
                    "Live control hooks": "future",
                }
            )
            st.markdown("### Active Tasks")
            if active_tasks:
                for task in active_tasks:
                    st.write(f"- {task}")
            else:
                st.caption("No active tasks recorded.")
            st.markdown("</div>", unsafe_allow_html=True)

    with tab_runs:
        st.markdown('<div class="tv-section">', unsafe_allow_html=True)
        if not runs:
            st.info("No run artifacts found.")
        else:
            rows = []
            for run in runs:
                rows.append(
                    {
                        "run": run["name"],
                        "test_auc": _metric_or_none(run, "test_auc"),
                        "valid_auc": _metric_or_none(run, "valid_auc"),
                        "test_logloss": _metric_or_none(run, "test_logloss"),
                        "valid_logloss": _metric_or_none(run, "valid_logloss"),
                        "feature_count": run["summary"].get("Feature count"),
                        "positive_rate": run["summary"].get("Positive rate"),
                        "walkforward_mean_valid_auc": run["walkforward_mean_valid_auc"],
                    }
                )
            runs_df = pd.DataFrame(rows)
            st.dataframe(runs_df, use_container_width=True, hide_index=True)

            selected = st.selectbox("Inspect run", [run["name"] for run in runs])
            run = next(item for item in runs if item["name"] == selected)
            st.markdown(f"### {run['name']}")
            dossier = run["path"] / "review_dossier.md"
            st.code(dossier.read_text() if dossier.exists() else "No review dossier yet.", language="markdown")
        st.markdown("</div>", unsafe_allow_html=True)

    with tab_data:
        st.markdown('<div class="tv-section">', unsafe_allow_html=True)
        if datasets.empty:
            st.info("No parquet files found under data/.")
        else:
            st.dataframe(datasets, use_container_width=True, hide_index=True)
            if "rows" in datasets.columns and datasets["rows"].notna().any():
                chart_df = datasets.dropna(subset=["rows"]).set_index("file")["rows"]
                st.bar_chart(chart_df)
        st.markdown("</div>", unsafe_allow_html=True)

    with tab_tasks:
        st.markdown('<div class="tv-section">', unsafe_allow_html=True)
        st.markdown("### Status counts")
        st.write(counts)
        left, right = st.columns(2)
        with left:
            st.markdown("#### Active")
            if active_tasks:
                for task in active_tasks:
                    st.write(f"- {task}")
            else:
                st.caption("None")
        with right:
            st.markdown("#### Pending")
            if pending_tasks:
                for task in pending_tasks[:20]:
                    st.write(f"- {task}")
            else:
                st.caption("None")
        st.markdown("</div>", unsafe_allow_html=True)

    with tab_future:
        st.markdown('<div class="tv-section">', unsafe_allow_html=True)
        st.markdown("### Expansion Path")
        st.write(
            [
                "1 orchestrator",
                "3 strategy namespaces",
                "3 trader workers",
                "1 shared risk layer",
                "read-only dashboard now, live controls later",
            ]
        )
        st.warning("Live controls are intentionally not wired yet. That is a feature, not a missing checkbox.")
        st.markdown("</div>", unsafe_allow_html=True)


if __name__ == "__main__":
    main()
