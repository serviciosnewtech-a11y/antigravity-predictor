"""
Deterministic fixture tests for the H-13 fail-loud feature parity gate
(src/feature_gate.py) and its integration into predictor_server.AssetEngine.

These are the primary required proof per TASK_BRIEF_FEATURE_SPRINT.md P1:
  - complete fixture passes
  - one missing feature blocks inference
  - one non-finite feature blocks inference
  - a missing feature family blocks inference
  - model.predict is not called when the gate fails
  - a healthy vector does reach the model call

No network, no docker, no live data — pure deterministic fixtures.
Run with:  python3 -m pytest tests/test_feature_gate.py -v
"""
import math
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import pytest

from feature_gate import evaluate_feature_parity, classify_feature_family


# ── The authoritative 126 trained feature names, extracted from the real
#    booster files by tools/p0_audit.py (all 6 models share this exact,
#    identically-ordered list). Kept inline here so this test suite has
#    zero dependency on lightgbm / model files being present.
FEATURE_NAMES = [
    "log_return_1","log_return_3","log_return_6","range_1","atr_proxy","volatility_lookback",
    "hour_of_day","day_of_week","session_asia","session_london","session_newyork","volume_zscore",
    "relative_volume","volume_percentile","body_ratio","upper_wick_ratio","lower_wick_ratio",
    "atr_normalized_range","stop_distance","dist_ema_fast","dist_ema_slow","trend_strength",
    "trend_direction","ema_slow_slope",
    "sweep_high_detected","sweep_low_detected","sweep_depth_atr","sweep_rejection_ratio",
    "sweep_volume_zscore","bullish_fvg_present","bearish_fvg_present","fvg_size_atr",
    "fvg_age_candles","price_inside_fvg","breakout_volume_confirmation",
    "rejection_volume_confirmation","volume_block_strength","atr_percentile","range_compression",
    "high_volatility_flag","market_regime",
    "funding_rate","funding_rate_abs","funding_rate_mean_4","funding_rate_std_4","mark_basis",
    "mark_premium","mark_premium_mean_4","futures_pressure",
    "m1_bull_ratio","m1_vol_tail_pct","m1_max_body_ratio","m1_trend","m1_atr_ratio","m1_volume_zscore",
    "m5_bull_ratio","m5_vol_tail_pct","m5_max_body_ratio","m5_trend","m5_atr_ratio","m5_volume_zscore",
    "btc_1h_log_return_1","btc_1h_log_return_3","btc_1h_ema_fast","btc_1h_ema_slow",
    "btc_1h_trend_strength","btc_1h_trend_dir","btc_1h_atr_pct","btc_1h_volume_zscore","btc_1h_regime",
    "btc_4h_log_return_1","btc_4h_log_return_3","btc_4h_ema_fast","btc_4h_ema_slow",
    "btc_4h_trend_strength","btc_4h_trend_dir","btc_4h_atr_pct","btc_4h_volume_zscore","btc_4h_regime",
    "btc_1d_log_return_1","btc_1d_log_return_3","btc_1d_ema_fast","btc_1d_ema_slow",
    "btc_1d_trend_strength","btc_1d_trend_dir","btc_1d_atr_pct","btc_1d_volume_zscore","btc_1d_regime",
    "eth_return_1","eth_return_3","eth_trend","eth_volume_block",
    "sol_return_1","sol_return_3","sol_trend","sol_volume_block",
    "gold_return_1d","gold_return_5d","gold_ema_fast","gold_ema_slow","gold_trend","gold_trend_dir",
    "oil_return_1d","oil_return_5d","oil_ema_fast","oil_ema_slow","oil_trend","oil_trend_dir",
    "dxy_return_1d","dxy_return_5d","dxy_ema_fast","dxy_ema_slow","dxy_trend","dxy_trend_dir",
    "spx_return_1d","spx_return_5d","spx_ema_fast","spx_ema_slow","spx_trend","spx_trend_dir",
    "vix_return_1d","vix_return_5d","vix_ema_fast","vix_ema_slow","vix_trend","vix_trend_dir",
]

assert len(FEATURE_NAMES) == 126, f"fixture drift: expected 126 names, got {len(FEATURE_NAMES)}"


def complete_fixture() -> dict:
    """A fully populated, finite value for every one of the 126 trained features."""
    return {name: 0.123 for name in FEATURE_NAMES}


# ── Pure gate-logic tests ────────────────────────────────────────────────────

def test_complete_fixture_passes():
    result = evaluate_feature_parity(FEATURE_NAMES, complete_fixture(), source_timestamp=1000.0)
    assert result.parity_ok is True
    assert result.missing == []
    assert result.invalid == []
    assert result.stale == []
    assert result.populated_features == 126
    assert result.expected_features == 126
    assert result.blocked_reason is None


def test_one_missing_feature_blocks():
    values = complete_fixture()
    del values["funding_rate"]
    result = evaluate_feature_parity(FEATURE_NAMES, values)
    assert result.parity_ok is False
    assert result.missing == ["funding_rate"]
    assert result.populated_features == 125
    assert "1 missing" in result.blocked_reason


def test_one_non_finite_feature_blocks():
    for bad in (float("nan"), float("inf"), float("-inf")):
        values = complete_fixture()
        values["btc_1h_ema_fast"] = bad
        result = evaluate_feature_parity(FEATURE_NAMES, values)
        assert result.parity_ok is False, f"non-finite value {bad} should block"
        assert "btc_1h_ema_fast" in result.invalid
        assert result.missing == []  # present, just invalid


def test_none_value_counts_as_missing_not_invalid():
    values = complete_fixture()
    values["gold_return_1d"] = None
    result = evaluate_feature_parity(FEATURE_NAMES, values)
    assert result.parity_ok is False
    assert "gold_return_1d" in result.missing
    assert "gold_return_1d" not in result.invalid


def test_missing_family_blocks():
    """Dropping an entire family (funding/mark/basis, 8 features) blocks
    inference and the family is reported as fully UNAVAILABLE, not merely
    degraded."""
    values = complete_fixture()
    funding_family = [n for n in FEATURE_NAMES if classify_feature_family(n) == "primary_futures_funding_mark_basis"]
    assert len(funding_family) == 8
    for name in funding_family:
        del values[name]
    result = evaluate_feature_parity(FEATURE_NAMES, values)
    assert result.parity_ok is False
    assert set(result.missing) == set(funding_family)
    fam_status = result.family_status["primary_futures_funding_mark_basis"]
    assert fam_status.status == "UNAVAILABLE"
    assert fam_status.total == 8
    assert fam_status.ok == 0


def test_stale_feature_blocks():
    values = complete_fixture()
    result = evaluate_feature_parity(FEATURE_NAMES, values, stale_features={"mark_basis"})
    assert result.parity_ok is False
    assert result.stale == ["mark_basis"]


def test_family_classification_matches_p0_audit_grouping():
    # Spot-check the family classification against tools/p0_audit.py findings.
    assert classify_feature_family("log_return_1") == "primary_basic"
    assert classify_feature_family("sweep_high_detected") == "primary_structure_smc"
    assert classify_feature_family("funding_rate") == "primary_futures_funding_mark_basis"
    assert classify_feature_family("m1_bull_ratio") == "microstructure_m1"
    assert classify_feature_family("m5_trend") == "microstructure_m5"
    assert classify_feature_family("btc_1h_regime") == "higher_tf_btc_1h"
    assert classify_feature_family("btc_4h_regime") == "higher_tf_btc_4h"
    assert classify_feature_family("btc_1d_regime") == "higher_tf_btc_1d"
    # Renamed from "cross_asset_eth_sol" -> "cross_asset_peers" when the
    # H-13 follow-up feature expansion added per-model peer columns (each
    # model's peers are whichever two of btc/eth/sol aren't its own asset,
    # so "eth_sol" wasn't accurate for e.g. the ETH model's btc_/sol_ cols).
    assert classify_feature_family("eth_trend") == "cross_asset_peers"
    assert classify_feature_family("sol_trend") == "cross_asset_peers"
    assert classify_feature_family("btc_trend") == "cross_asset_peers"
    assert classify_feature_family("gold_trend_dir") == "macro_gold"
    assert classify_feature_family("oil_trend_dir") == "macro_oil"
    assert classify_feature_family("dxy_trend_dir") == "macro_dxy"
    assert classify_feature_family("spx_trend_dir") == "macro_spx"
    assert classify_feature_family("vix_trend_dir") == "macro_vix"


def test_full_126_feature_classification_covers_known_h13_split():
    """Reproduces the P0 audit's headline finding as a regression guard:
    with nothing wired beyond the primary asset's own 15m OHLCV features,
    41 features are LIVE-able and 85 fall into zero-fill-prone families."""
    live_capable = {"primary_basic", "primary_structure_smc"}
    live_count = sum(1 for n in FEATURE_NAMES if classify_feature_family(n) in live_capable)
    zero_fill_prone_count = len(FEATURE_NAMES) - live_count
    assert live_count == 41
    assert zero_fill_prone_count == 85


# ── AssetEngine integration tests (model.predict must not be called) ───────

class _RecordingBooster:
    """Stand-in for lgb.Booster — records whether predict() was invoked."""
    def __init__(self, feature_names):
        self._feature_names = list(feature_names)
        self.predict_calls = 0

    def feature_name(self):
        return list(self._feature_names)

    def predict(self, X):
        self.predict_calls += 1
        # Return a deterministic constant prediction for every row.
        return [0.5] * len(X)


def _make_candles(n=60):
    """Deterministic synthetic OHLCV candles, monotonically increasing time."""
    candles = []
    price = 100.0
    for i in range(n):
        o = price
        c = price * (1.0 + (0.001 if i % 2 == 0 else -0.0008))
        h = max(o, c) * 1.001
        l = min(o, c) * 0.999
        v = 10.0 + (i % 5)
        candles.append({"time": 1_700_000_000 + i * 900, "open": o, "high": h, "low": l, "close": c, "volume": v})
        price = c
    return candles


@pytest.fixture()
def engine_with_stub_models(monkeypatch):
    """Build a real AssetEngine wired to _RecordingBooster stand-ins instead
    of real lightgbm boosters, and stub out the async broadcast so
    _run_prediction can be exercised synchronously without an event loop."""
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    import predictor_server as ps

    cfg = {
        "model_long_path": "unused", "model_short_path": "unused",
        "buy_threshold": 0.6, "exit_threshold": 0.4, "sell_threshold": 0.6,
        "exit_short_threshold": 0.4, "tp_atr_mult": 1.5, "sl_atr_mult": 1.0,
        "spread_offset_pct": 0.0002, "max_candles_held": 4,
    }
    eng = ps.AssetEngine("TEST/USDT", cfg)
    eng.model_long = _RecordingBooster(FEATURE_NAMES)
    eng.model_short = _RecordingBooster(FEATURE_NAMES)
    eng.feature_names = FEATURE_NAMES
    eng.candles = _make_candles(60)

    # _run_prediction calls asyncio.run_coroutine_threadsafe(..., loop); avoid
    # needing a real event loop in a synchronous test by making broadcast a
    # no-op scheduling shim.
    monkeypatch.setattr(ps.asyncio, "run_coroutine_threadsafe", lambda coro, loop: coro.close())
    # fetch_btc_htf_context() makes a real network call to Bybit; stub it to
    # a deterministic None so these tests don't depend on/vary with sandbox
    # network reachability (matches _build_funding_ctx/_build_peer_ctx, which
    # are already None in this fixture with no live cross-engine state wired).
    monkeypatch.setattr(ps, "fetch_btc_htf_context", lambda: None)
    return ps, eng


def test_healthy_vector_reaches_model_call(engine_with_stub_models):
    """build_features() on real primary-asset OHLCV populates only the
    41 'live-capable' families; the other 85 trained features are absent
    from a bare synthetic model here, so on its own this fixture would be
    blocked. To prove the 'healthy path reaches the model' requirement
    deterministically, monkeypatch build_features to return every trained
    feature already populated for this one test."""
    ps, eng = engine_with_stub_models
    import pandas as pd

    orig = ps.build_features

    def _all_features_build(candles, funding_ctx=None, peer_ctx=None, htf_ctx=None):
        df = orig(candles, funding_ctx=funding_ctx, peer_ctx=peer_ctx, htf_ctx=htf_ctx)
        for name in FEATURE_NAMES:
            if name not in df.columns:
                df[name] = 0.42
        return df

    ps.build_features = _all_features_build
    try:
        eng._run_prediction(confirm=True, loop=None)
    finally:
        ps.build_features = orig

    assert eng.degraded is False
    assert eng.missing_features == []
    assert eng.model_long.predict_calls == 1
    assert eng.model_short.predict_calls == 1
    assert eng.latest_signal in {"BUY", "SELL", "NEUTRAL", "EXIT"}
    assert eng.feature_gate_result["parity_status"] == "PASS"


def test_degraded_vector_blocks_model_call_and_signal(engine_with_stub_models):
    """Using the REAL build_features() against real primary-asset OHLCV
    (no monkeypatching): this reproduces the actual H-13 condition — 85 of
    126 trained features are absent — and must block inference."""
    ps, eng = engine_with_stub_models
    eng._run_prediction(confirm=True, loop=None)

    assert eng.degraded is True
    assert len(eng.missing_features) == 85
    assert eng.model_long.predict_calls == 0, "model must NOT be called on a degraded vector"
    assert eng.model_short.predict_calls == 0, "model must NOT be called on a degraded vector"
    assert eng.latest_signal == "UNAVAILABLE"
    assert eng.feature_gate_result["parity_status"] == "FAIL"
    assert eng.inference_blocked_count == 1


def test_non_finite_injected_feature_blocks_even_if_present(engine_with_stub_models, monkeypatch):
    """If build_features() ever produced a NaN/inf for a feature it does
    compute, the gate must still block — not silently pass a poisoned
    value into the model."""
    ps, eng = engine_with_stub_models

    orig = ps.build_features

    def _poisoned_build(candles, funding_ctx=None, peer_ctx=None, htf_ctx=None):
        df = orig(candles, funding_ctx=funding_ctx, peer_ctx=peer_ctx, htf_ctx=htf_ctx)
        for name in FEATURE_NAMES:
            if name not in df.columns:
                df[name] = 0.42
        df.loc[df.index[-1], "log_return_1"] = float("nan")
        return df

    ps.build_features = _poisoned_build
    try:
        eng._run_prediction(confirm=True, loop=None)
    finally:
        ps.build_features = orig

    assert eng.degraded is True
    assert eng.model_long.predict_calls == 0
    assert eng.model_short.predict_calls == 0
    assert "log_return_1" in eng.feature_gate_result["invalid_features"]


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
