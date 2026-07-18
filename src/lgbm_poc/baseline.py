from __future__ import annotations

from dataclasses import asdict, dataclass


@dataclass(frozen=True)
class BaselineSpec:
    symbol: str = "BTC/USDT"
    timeframe: str = "15m"
    market_type: str = "spot"
    horizon_bars: int = 4
    tp_pct: float = 0.003
    sl_pct: float = 0.002
    sweep_lookback: int = 20
    volume_block_window: int = 20
    lookback_window: int = 20
    limit: int = 1000
    max_rows: int = 10000


BASELINE = BaselineSpec()


def baseline_dict() -> dict:
    return asdict(BASELINE)
