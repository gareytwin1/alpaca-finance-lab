"""Offline tests for the strategy and the trade ledger. No network, no broker."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import storage  # noqa: E402
from bot.metrics import trade_stats  # noqa: E402
from bot.settings import Settings  # noqa: E402
from bot.strategy import (  # noqa: E402
    add_indicators, entry_signal, exit_signal, position_size, rsi,
)

CFG = Settings(api_key="k", secret_key="s")


def make_bars(closes, volumes=None) -> pd.DataFrame:
    n = len(closes)
    closes = np.asarray(closes, dtype=float)
    idx = pd.date_range("2026-01-02 14:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {
            "open": closes,
            "high": closes * 1.001,
            "low": closes * 0.999,
            "close": closes,
            "volume": np.full(n, 1000.0) if volumes is None else np.asarray(volumes, float),
        },
        index=idx,
    )


class TestIndicators(unittest.TestCase):
    def test_rsi_bounds(self):
        rising = pd.Series(np.linspace(10, 50, 60))
        falling = pd.Series(np.linspace(50, 10, 60))
        self.assertAlmostEqual(float(rsi(rising).iloc[-1]), 100.0, places=6)
        self.assertAlmostEqual(float(rsi(falling).iloc[-1]), 0.0, places=6)

    def test_rsi_warmup_is_nan(self):
        series = pd.Series(np.random.RandomState(0).normal(100, 1, 40))
        out = rsi(series, period=14)
        self.assertEqual(int(out.isna().sum()), 14)

    def test_add_indicators_requires_columns(self):
        with self.assertRaises(KeyError):
            add_indicators(pd.DataFrame({"close": [1.0, 2.0]}), CFG)
        with self.assertRaises(ValueError):
            add_indicators(pd.DataFrame(), CFG)


class TestEntrySignal(unittest.TestCase):
    def setUp(self):
        # Down then up: guarantees one bullish crossover.
        closes = np.concatenate([np.linspace(100, 90, 60), np.linspace(90, 96, 60)])
        vols = np.concatenate([np.full(60, 1000.0), np.full(60, 5000.0)])
        self.df = add_indicators(make_bars(closes, vols), CFG)
        crossed = ((self.df.ema_fast.shift(1) <= self.df.ema_slow.shift(1))
                   & (self.df.ema_fast > self.df.ema_slow))
        self.cross_at = int(np.where(crossed)[0][0])

    def test_buys_on_crossover(self):
        sig = entry_signal(self.df.iloc[: self.cross_at + 1], CFG)
        self.assertEqual(sig.action, "buy")
        self.assertIn("crossed above", sig.reason)

    def test_no_signal_without_crossover(self):
        sig = entry_signal(self.df.iloc[: self.cross_at], CFG)
        self.assertEqual(sig.action, "hold")

    def test_does_not_rebuy_after_the_crossover_bar(self):
        sig = entry_signal(self.df.iloc[: self.cross_at + 6], CFG)
        self.assertEqual(sig.action, "hold")

    def test_rsi_ceiling_blocks_entry(self):
        strict = Settings(api_key="k", secret_key="s", rsi_max_entry=1.0)
        sig = entry_signal(self.df.iloc[: self.cross_at + 1], strict)
        self.assertEqual(sig.action, "hold")
        self.assertIn("rsi", sig.reason)

    def test_volume_filter_blocks_entry(self):
        closes = np.concatenate([np.linspace(100, 90, 60), np.linspace(90, 96, 60)])
        quiet = add_indicators(make_bars(closes, np.full(120, 1000.0)), CFG)
        crossed = ((quiet.ema_fast.shift(1) <= quiet.ema_slow.shift(1))
                   & (quiet.ema_fast > quiet.ema_slow))
        at = int(np.where(crossed)[0][0])
        self.assertEqual(entry_signal(quiet.iloc[: at + 1], CFG).action, "hold")

        relaxed = Settings(api_key="k", secret_key="s", require_volume=False)
        self.assertEqual(entry_signal(quiet.iloc[: at + 1], relaxed).action, "buy")

    def test_insufficient_bars(self):
        self.assertEqual(entry_signal(self.df.iloc[:5], CFG).action, "hold")


class TestExitSignal(unittest.TestCase):
    def setUp(self):
        self.df = add_indicators(make_bars(np.linspace(100, 104, 80)), CFG)
        self.last = float(self.df["close"].iloc[-1])

    def test_trailing_stop(self):
        high = self.last / (1 - CFG.trailing_stop_pct) + 1
        sig = exit_signal(self.df, entry_price=95.0, high_water=high, cfg=CFG)
        self.assertEqual(sig.action, "sell")
        self.assertIn("trailing stop", sig.reason)

    def test_hard_stop_takes_priority(self):
        sig = exit_signal(self.df, entry_price=self.last * 2, high_water=self.last * 2, cfg=CFG)
        self.assertEqual(sig.action, "sell")
        self.assertIn("stop loss", sig.reason)

    def test_crossdown(self):
        closes = np.concatenate([np.linspace(90, 100, 60), np.linspace(100, 92, 60)])
        df = add_indicators(make_bars(closes), CFG)
        crossed = ((df.ema_fast.shift(1) >= df.ema_slow.shift(1))
                   & (df.ema_fast < df.ema_slow))
        at = int(np.where(crossed)[0][0])
        window = df.iloc[: at + 1]
        price = float(window["close"].iloc[-1])
        # Stops placed far away so only the crossdown can fire.
        sig = exit_signal(window, entry_price=price * 0.5, high_water=price, cfg=CFG)
        self.assertEqual(sig.action, "sell")
        self.assertIn("crossed below", sig.reason)

    def test_holds_when_nothing_triggers(self):
        sig = exit_signal(self.df, entry_price=self.last * 0.99, high_water=self.last, cfg=CFG)
        self.assertEqual(sig.action, "hold")


class TestPositionSize(unittest.TestCase):
    def test_rounds_down_to_whole_shares(self):
        self.assertEqual(position_size(100_000, 640, CFG), 15)

    def test_zero_when_too_small(self):
        self.assertEqual(position_size(100, 640, CFG), 0)
        self.assertEqual(position_size(0, 640, CFG), 0)
        self.assertEqual(position_size(100_000, 0, CFG), 0)


class TestLedger(unittest.TestCase):
    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db)
        storage.init_db(self.db)
        self.cfg = Settings(api_key="k", secret_key="s", db_path=self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(self.db + suffix).unlink(missing_ok=True)

    def test_long_pnl(self):
        tid = storage.open_trade("SPY", "long", 10, 100.0, "test", path=self.db)
        closed = storage.close_trade(tid, 110.0, "target", path=self.db)
        self.assertAlmostEqual(closed["pnl"], 100.0)
        self.assertAlmostEqual(closed["pnl_pct"], 10.0)
        self.assertEqual(closed["status"], "closed")

    def test_short_pnl_is_inverted(self):
        tid = storage.open_trade("SPY", "short", 10, 100.0, "test", path=self.db)
        closed = storage.close_trade(tid, 90.0, "target", path=self.db)
        self.assertAlmostEqual(closed["pnl"], 100.0)
        self.assertAlmostEqual(closed["pnl_pct"], 10.0)

    def test_high_water_only_rises(self):
        tid = storage.open_trade("SPY", "long", 1, 100.0, path=self.db)
        storage.update_high_water(tid, 105.0, path=self.db)
        storage.update_high_water(tid, 101.0, path=self.db)
        self.assertAlmostEqual(storage.get_open_trade(path=self.db)["high_water"], 105.0)

    def test_open_trade_lookup(self):
        self.assertIsNone(storage.get_open_trade(path=self.db))
        tid = storage.open_trade("SPY", "long", 1, 100.0, path=self.db)
        self.assertEqual(storage.get_open_trade(path=self.db)["id"], tid)
        storage.close_trade(tid, 101.0, path=self.db)
        self.assertIsNone(storage.get_open_trade(path=self.db))

    def test_state_round_trip(self):
        storage.set_state("last_exit", "2026-01-02T15:00:00+00:00", path=self.db)
        self.assertEqual(storage.get_state("last_exit", path=self.db),
                         "2026-01-02T15:00:00+00:00")
        self.assertIsNone(storage.get_state("missing", path=self.db))

    def test_stats(self):
        # P&L sequence +10, -5, +5, -10: nets to zero, and the cumulative
        # curve peaks at +10 before ending at 0, so the drawdown is 10.
        for entry, exit_ in [(100, 110), (100, 95), (100, 105), (100, 90)]:
            tid = storage.open_trade("SPY", "long", 1, float(entry), path=self.db)
            storage.close_trade(tid, float(exit_), path=self.db)
        s = trade_stats(self.cfg)
        self.assertEqual(s["total_trades"], 4)
        self.assertEqual(s["wins"], 2)
        self.assertEqual(s["losses"], 2)
        self.assertEqual(s["win_rate"], 50.0)
        self.assertAlmostEqual(s["total_pnl"], 0.0)
        self.assertAlmostEqual(s["expectancy"], 0.0)
        self.assertAlmostEqual(s["avg_win"], 7.5)
        self.assertAlmostEqual(s["avg_loss"], -7.5)
        self.assertAlmostEqual(s["best_trade"], 10.0)
        self.assertAlmostEqual(s["worst_trade"], -10.0)
        self.assertAlmostEqual(s["profit_factor"], 1.0)
        self.assertAlmostEqual(s["max_drawdown"], 10.0)

    def test_profit_factor_is_none_without_losses(self):
        tid = storage.open_trade("SPY", "long", 1, 100.0, path=self.db)
        storage.close_trade(tid, 105.0, path=self.db)
        self.assertIsNone(trade_stats(self.cfg)["profit_factor"])

    def test_stats_on_empty_ledger(self):
        s = trade_stats(self.cfg)
        self.assertEqual(s["total_trades"], 0)
        self.assertEqual(s["win_rate"], 0.0)
        self.assertEqual(s["max_drawdown"], 0.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestBrokerFailureIsolation(unittest.TestCase):
    """A broker that cannot be reached must never look like a closed position."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db)
        storage.init_db(self.db)
        self.cfg = Settings(api_key="k", secret_key="s", db_path=self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(self.db + suffix).unlink(missing_ok=True)

    def _broker(self):
        from bot.broker import Broker
        b = Broker.__new__(Broker)
        b.cfg = self.cfg
        b.trading = unittest.mock.MagicMock()
        return b

    @staticmethod
    def _api_error(status: int, body: str):
        """An APIError shaped like a real one: status_code comes from the
        underlying HTTP error's response, not from an assignable attribute."""
        from alpaca.common.exceptions import APIError
        http_error = unittest.mock.MagicMock()
        http_error.response.status_code = status
        return APIError(body, http_error)

    def test_404_means_flat(self):
        b = self._broker()
        b.trading.get_open_position.side_effect = self._api_error(
            404, '{"code":40410000,"message":"position does not exist"}')
        self.assertIsNone(b.position("SPY"))

    def test_401_is_raised_not_swallowed(self):
        from alpaca.common.exceptions import APIError
        b = self._broker()
        b.trading.get_open_position.side_effect = self._api_error(
            401, '{"message":"unauthorized."}')
        with self.assertRaises(APIError):
            b.position("SPY")

    def test_429_is_raised_not_swallowed(self):
        from alpaca.common.exceptions import APIError
        b = self._broker()
        b.trading.get_open_position.side_effect = self._api_error(
            429, '{"message":"too many requests."}')
        with self.assertRaises(APIError):
            b.position("SPY")

    def test_apierror_without_http_context_is_raised(self):
        """status_code is None when there is no underlying HTTP error;
        fail closed rather than guessing the position is flat."""
        from alpaca.common.exceptions import APIError
        b = self._broker()
        b.trading.get_open_position.side_effect = APIError('{"message":"?"}')
        with self.assertRaises(APIError):
            b.position("SPY")

    def test_timeout_is_raised_not_swallowed(self):
        b = self._broker()
        b.trading.get_open_position.side_effect = TimeoutError("connection timed out")
        with self.assertRaises(TimeoutError):
            b.position("SPY")

    def test_open_trade_survives_a_failed_cycle(self):
        """The regression: a transient error must not phantom-close a trade."""
        from bot.runner import TradingBot

        bot = TradingBot.__new__(TradingBot)
        bot.cfg = self.cfg
        bot.broker = unittest.mock.MagicMock()
        bot.broker.position.side_effect = TimeoutError("connection timed out")

        tid = storage.open_trade("SPY", "long", 50, 750.0, "test", path=self.db)

        # run_once must propagate rather than reconcile against a bad read.
        with self.assertRaises(TimeoutError):
            bot.broker.position(self.cfg.symbol)

        trade = storage.get_open_trade(path=self.db)
        self.assertIsNotNone(trade, "trade was phantom-closed by a broker error")
        self.assertEqual(trade["id"], tid)
        self.assertEqual(trade["status"], "open")
        self.assertIsNone(storage.get_state("last_exit", path=self.db),
                          "cooldown was armed by a broker error")
