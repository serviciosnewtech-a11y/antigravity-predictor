"""
H-13 remediation — fail-loud feature parity gate.

Pure, deterministic, dependency-light module used by predictor_server.py
(and by tests) to decide whether a live feature vector is trustworthy
enough to feed into a trained LightGBM booster.

Design rules (per TASK_BRIEF_FEATURE_SPRINT.md v2.1 / P1):
  - Missing trained features are NEVER silently replaced with 0.0.
  - Non-finite values (NaN, +inf, -inf) are treated as invalid, not usable.
  - Stale values (caller-supplied, based on source data age) are treated
    as unusable even if numerically present.
  - Any of the above blocks inference outright: the caller must not call
    the model and must emit an explicit UNAVAILABLE/degraded state instead
    of BUY/SELL/NEUTRAL/EXIT.
  - Classification is grouped by feature "family" (funding/mark/basis,
    microstructure, higher-timeframe regime, cross-asset, macro, ...) so
    operators can see which subsystem is degraded at a glance.

This module does not fetch data, does not train models, and does not
change thresholds or risk logic. It only judges whether an already-built
feature row is complete and sane.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

# ── Feature family classification ───────────────────────────────────────────
# Mirrors the family grouping established in tools/p0_audit.py (P0 audit,
# H-13). Classification is prefix/name based so it works for any of the
# BTC/ETH/SOL long/short boosters, which all share the same 126 trained
# feature names.

_FAMILY_RULES: List[tuple[str, tuple[str, ...]]] = [
    ("primary_futures_funding_mark_basis", ("funding_rate", "mark_basis", "mark_premium", "futures_pressure")),
    ("microstructure_m1", ("m1_",)),
    ("microstructure_m5", ("m5_",)),
    ("higher_tf_btc_1h", ("btc_1h_",)),
    ("higher_tf_btc_4h", ("btc_4h_",)),
    ("higher_tf_btc_1d", ("btc_1d_",)),
    # Cross-asset peer context (feature-expansion follow-up to H-13): each
    # model's peer columns are named for whichever two of {btc,eth,sol}
    # aren't its own asset (e.g. the ETH model has btc_return_1/sol_return_1,
    # not eth_return_1). Matched generically here since the specific pair
    # differs per model; checked after the more specific higher_tf_btc_*
    # rules above so btc_1h_/btc_4h_/btc_1d_ columns aren't miscategorized.
    ("cross_asset_peers", ("btc_", "eth_", "sol_")),
    ("macro_gold", ("gold_",)),
    ("macro_oil", ("oil_",)),
    ("macro_dxy", ("dxy_",)),
    ("macro_spx", ("spx_",)),
    ("macro_vix", ("vix_",)),
    ("primary_structure_smc", (
        "sweep_", "bullish_fvg", "bearish_fvg", "fvg_", "price_inside_fvg",
        "breakout_volume_confirmation", "rejection_volume_confirmation",
        "volume_block_strength", "atr_percentile", "range_compression",
        "high_volatility_flag", "market_regime",
    )),
]


def classify_feature_family(name: str) -> str:
    """Return the family a trained feature name belongs to.

    Falls back to "primary_basic" for the single-asset 15m OHLCV-derived
    features (price/return, volatility, session/time, volume, candle shape,
    EMA/trend) that have no distinguishing prefix.
    """
    for family, prefixes in _FAMILY_RULES:
        if any(name == p or name.startswith(p) for p in prefixes):
            return family
    return "primary_basic"


# ── Result types ─────────────────────────────────────────────────────────────

@dataclass
class FamilyStatus:
    family: str
    total: int
    ok: int
    missing: List[str] = field(default_factory=list)
    invalid: List[str] = field(default_factory=list)
    stale: List[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if not self.missing and not self.invalid and not self.stale:
            return "OK"
        if len(self.missing) + len(self.invalid) + len(self.stale) >= self.total:
            return "UNAVAILABLE"
        return "DEGRADED"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "family": self.family,
            "total": self.total,
            "ok": self.ok,
            "missing": list(self.missing),
            "invalid": list(self.invalid),
            "stale": list(self.stale),
            "status": self.status,
        }


@dataclass
class ParityResult:
    expected_features: int
    populated_features: int
    missing: List[str]
    invalid: List[str]
    stale: List[str]
    family_status: Dict[str, FamilyStatus]
    source_timestamp: Optional[float] = None

    @property
    def parity_ok(self) -> bool:
        return not self.missing and not self.invalid and not self.stale

    @property
    def blocked_reason(self) -> Optional[str]:
        if self.parity_ok:
            return None
        parts = []
        if self.missing:
            parts.append(f"{len(self.missing)} missing")
        if self.invalid:
            parts.append(f"{len(self.invalid)} non-finite")
        if self.stale:
            parts.append(f"{len(self.stale)} stale")
        return "feature parity failed: " + ", ".join(parts)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "expected_features": self.expected_features,
            "populated_features": self.populated_features,
            "missing_features": list(self.missing),
            "invalid_features": list(self.invalid),
            "stale_features": list(self.stale),
            "parity_status": "PASS" if self.parity_ok else "FAIL",
            "blocked_reason": self.blocked_reason,
            "source_timestamp": self.source_timestamp,
            "family_status": {k: v.to_dict() for k, v in self.family_status.items()},
        }


def _is_finite_number(value: Any) -> bool:
    if value is None:
        return False
    try:
        f = float(value)
    except (TypeError, ValueError):
        return False
    return math.isfinite(f)


def evaluate_feature_parity(
    feature_names: Iterable[str],
    values: Mapping[str, Any],
    stale_features: Optional[Set[str]] = None,
    source_timestamp: Optional[float] = None,
) -> ParityResult:
    """Evaluate whether `values` fully and validly populates `feature_names`.

    Args:
        feature_names: the authoritative, ordered list of trained feature
            names for the active model (booster.feature_name()).
        values: mapping of feature name -> value for the row about to be
            scored. A name absent from `values` (or explicitly None) is
            treated as MISSING. A present but non-finite value (NaN, inf,
            -inf, or non-numeric) is treated as INVALID.
        stale_features: optional set of feature names the caller has
            determined are backed by data older than the allowed freshness
            window. These are reported as STALE even if numerically valid.
        source_timestamp: optional epoch seconds of the underlying candle/
            tick used to build `values`, recorded for diagnostics.

    Returns:
        ParityResult with parity_ok True only if there is zero missing,
        zero invalid, and zero stale features.
    """
    stale_features = stale_features or set()
    feature_names = list(feature_names)

    missing: List[str] = []
    invalid: List[str] = []
    stale: List[str] = []
    populated = 0

    family_buckets: Dict[str, FamilyStatus] = {}

    for name in feature_names:
        family = classify_feature_family(name)
        if family not in family_buckets:
            family_buckets[family] = FamilyStatus(family=family, total=0, ok=0)
        fam = family_buckets[family]
        fam.total += 1

        present = name in values and values[name] is not None
        if not present:
            missing.append(name)
            fam.missing.append(name)
            continue

        if name in stale_features:
            stale.append(name)
            fam.stale.append(name)
            continue

        if not _is_finite_number(values[name]):
            invalid.append(name)
            fam.invalid.append(name)
            continue

        populated += 1
        fam.ok += 1

    return ParityResult(
        expected_features=len(feature_names),
        populated_features=populated,
        missing=missing,
        invalid=invalid,
        stale=stale,
        family_status=family_buckets,
        source_timestamp=source_timestamp,
    )


def format_gate_log_summary(result: ParityResult) -> str:
    """One-line, log-friendly summary grouped by affected family."""
    if result.parity_ok:
        return f"parity=PASS {result.populated_features}/{result.expected_features} features"
    bad_families = [
        f"{fs.family}({len(fs.missing)}missing/{len(fs.invalid)}invalid/{len(fs.stale)}stale)"
        for fs in result.family_status.values()
        if fs.status != "OK"
    ]
    return (
        f"parity=FAIL {result.populated_features}/{result.expected_features} features populated; "
        f"affected families: {', '.join(bad_families)}"
    )
