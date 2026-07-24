"""
tests/test_candle_history_depth.py — regression test for a real live gap
found 2026-07-23 on a freshly rehearsed install: AssetEngine.fetch_initial_
candles() hardcoded `limit=150` on its Bybit kline request (~1.5 days of
15m candles), so every fresh start/restart only ever had about a day of
chart history to show on the dashboard's default (15m) view -- not because
anything was broken, just because nothing ever asked Bybit for more.
/api/candles for the model's native 15m timeframe serves directly from this
engine buffer (see get_candles() in predictor_server.py), so this cap
applied to the whole dashboard's default chart view, not just this one
function.

Fix: limit=1000, Bybit's own per-request max for this endpoint (same cap
fetch_display_candles() already clamps to) -- one request, no pagination,
~10.4 days of 15m history instead of ~1.5.
"""
import os
import sys
from unittest.mock import MagicMock, patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import predictor_server as ps  # noqa: E402


def test_fetch_initial_candles_requests_bybit_max_limit():
    engine = ps.AssetEngine("BTC/USDT", {"model_long_path": "x", "model_short_path": "y"})

    fake_response = MagicMock()
    fake_response.json.return_value = {"retCode": 0, "result": {"list": []}}

    with patch.object(ps, "requests") as mock_requests:
        mock_requests.get.return_value = fake_response
        engine.fetch_initial_candles()

    assert mock_requests.get.called
    (url,), _kwargs = mock_requests.get.call_args
    assert "limit=1000" in url, (
        f"expected the initial candle fetch to request Bybit's max of 1000 "
        f"candles (~10.4 days at 15m), got: {url!r} -- this is the exact "
        f"live bug where a fresh install only ever showed ~1.5 days of "
        f"chart history"
    )
    assert "limit=150" not in url
