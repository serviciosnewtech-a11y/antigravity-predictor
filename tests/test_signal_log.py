"""
tests/test_signal_log.py — verifies signal_log.py actually survives a
restart, since that's the entire point of it existing: AssetEngine's old
in-memory-only trades_history silently lost everything on every process
restart, and this module exists to make that stop being true.
"""
import importlib
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _fresh_signal_log(tmp_path, monkeypatch):
    """Import signal_log pointed at a throwaway logs/ dir, simulating a
    fresh process — this is what a real restart looks like from the
    module's point of view."""
    monkeypatch.setenv("LOGS_DIR", str(tmp_path))
    sys.modules.pop("signal_log", None)
    import signal_log
    importlib.reload(signal_log)
    signal_log.init_db()
    return signal_log


def test_trade_survives_reimport(tmp_path, monkeypatch):
    sl = _fresh_signal_log(tmp_path, monkeypatch)

    trade = {
        "symbol": "BTC/USDT", "type": "LONG",
        "entry_time": 1000, "exit_time": 1900,
        "entry_price": 65000.0, "exit_price": 65500.0,
        "pnl": 50.0, "pnl_pct": 0.0077, "reason": "Take Profit",
    }
    sl.record_trade(trade)

    # Simulate a real process restart: re-import the module fresh against
    # the same on-disk DB path, exactly like a new predictor process would.
    monkeypatch.setenv("LOGS_DIR", str(tmp_path))
    sys.modules.pop("signal_log", None)
    import signal_log as sl2
    importlib.reload(sl2)

    rows = sl2.get_trades(symbol="BTC/USDT")
    assert len(rows) == 1
    assert rows[0]["entry_price"] == 65000.0
    assert rows[0]["exit_reason"] == "Take Profit"

    stats = sl2.get_stats(symbol="BTC/USDT")
    assert stats["total_trades"] == 1
    assert stats["win_trades"] == 1
    assert stats["loss_trades"] == 0
    assert stats["total_pnl"] == 50.0


def test_signal_event_survives_reimport(tmp_path, monkeypatch):
    sl = _fresh_signal_log(tmp_path, monkeypatch)

    sl.record_signal_event(ts=1000, symbol="ETH/USDT", signal="BUY",
                            long_prob=0.25, short_prob=0.05,
                            price=3200.0, atr=15.0, degraded=False)
    sl.record_signal_event(ts=1900, symbol="ETH/USDT", signal="NEUTRAL",
                            long_prob=0.10, short_prob=0.08,
                            price=3210.0, atr=14.5, degraded=False)

    monkeypatch.setenv("LOGS_DIR", str(tmp_path))
    sys.modules.pop("signal_log", None)
    import signal_log as sl2
    importlib.reload(sl2)

    events = sl2.get_signal_events(symbol="ETH/USDT")
    assert len(events) == 2
    # newest-first
    assert events[0]["signal"] == "NEUTRAL"
    assert events[1]["signal"] == "BUY"


def test_losing_trade_counted_correctly(tmp_path, monkeypatch):
    sl = _fresh_signal_log(tmp_path, monkeypatch)
    sl.record_trade({
        "symbol": "SOL/USDT", "type": "SHORT",
        "entry_time": 1000, "exit_time": 1300,
        "entry_price": 80.0, "exit_price": 82.0,
        "pnl": -25.0, "pnl_pct": -0.025, "reason": "Stop Loss",
    })
    stats = sl.get_stats(symbol="SOL/USDT")
    assert stats["win_trades"] == 0
    assert stats["loss_trades"] == 1
    assert stats["total_pnl"] == -25.0


def test_predictor_server_update_sim_writes_through_to_signal_log(tmp_path, monkeypatch):
    """Integration test, not just signal_log.py in isolation: proves
    AssetEngine._update_sim() (the actual live code path that closes a
    simulated trade) really does write through to signal_log, and that
    AssetEngine.load_history() really does read it back — the two things
    this whole module exists for. Exercises the real predictor_server.py
    code, not a reimplementation of it."""
    monkeypatch.setenv("LOGS_DIR", str(tmp_path))
    sys.modules.pop("signal_log", None)
    sys.modules.pop("predictor_server", None)
    import predictor_server as ps
    ps.signal_log.init_db()

    cfg = {
        "model_long_path": "unused", "model_short_path": "unused",
        "buy_threshold": 0.6, "exit_threshold": 0.4, "sell_threshold": 0.6,
        "exit_short_threshold": 0.4, "tp_atr_mult": 1.5, "sl_atr_mult": 1.0,
        "spread_offset_pct": 0.0002, "max_candles_held": 4,
    }
    eng = ps.AssetEngine("BTC/USDT", cfg)

    # Open a long position directly (bypassing the model-inference path,
    # which isn't what's under test here), then feed a price that hits TP.
    eng.position = {
        "type": "LONG", "entry_time": 1_700_000_000, "entry_price": 100.0,
        "tp": 101.5, "sl": 99.0, "candles_held": 0,
    }
    eng._update_sim(price=102.0, ts=1_700_000_900, atr=1.0, confirm=True)

    assert eng.position is None, "position should have closed on hitting TP"
    assert len(eng.trades_history) == 1
    assert eng.trades_history[0]["reason"] == "Take Profit"

    # This is the actual point of the module: a brand new engine (simulating
    # a fresh process after a restart) should recover this trade from disk,
    # not start blank.
    fresh_eng = ps.AssetEngine("BTC/USDT", cfg)
    assert fresh_eng.trades_history == [], "sanity check: starts empty before load_history()"
    fresh_eng.load_history()
    assert len(fresh_eng.trades_history) == 1
    assert fresh_eng.trades_history[0]["reason"] == "Take Profit"
    assert fresh_eng.win_trades == 1
    assert fresh_eng.total_pnl > 0


def test_empty_history_is_not_an_error(tmp_path, monkeypatch):
    """A fresh install with zero prior trades should return clean empty
    results, not raise — signal_log.get_stats()/get_trades() are called
    unconditionally on every startup regardless of whether there's any
    history yet."""
    sl = _fresh_signal_log(tmp_path, monkeypatch)
    assert sl.get_trades(symbol="BTC/USDT") == []
    assert sl.get_signal_events(symbol="BTC/USDT") == []
    stats = sl.get_stats(symbol="BTC/USDT")
    assert stats == {"total_trades": 0, "win_trades": 0, "loss_trades": 0, "total_pnl": 0.0}
