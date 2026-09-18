"""Offline tests for the strategy and the trade ledger. No network, no broker."""

from __future__ import annotations

import logging
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

# The modules under test log warnings and errors by design; the
# assertions cover that behaviour, so keep the test output readable.
logging.disable(logging.CRITICAL)

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


class TestOrderStatusLookup(unittest.TestCase):
    """get_order / wait_for_fill must never turn an outage into an outcome."""

    def _broker(self):
        from bot.broker import Broker
        b = Broker.__new__(Broker)
        b.cfg = Settings(api_key="k", secret_key="s")
        b.trading = unittest.mock.MagicMock()
        return b

    @staticmethod
    def _api_error(status, body='{"message":"x"}'):
        from alpaca.common.exceptions import APIError
        http_error = unittest.mock.MagicMock()
        http_error.response.status_code = status
        return APIError(body, http_error)

    @staticmethod
    def _order(status, filled_qty=0, avg=None):
        o = unittest.mock.MagicMock()
        o.id, o.status = "ord-1", status
        o.filled_qty, o.filled_avg_price = filled_qty, avg
        return o

    # -- get_order ------------------------------------------------------

    def test_404_returns_none(self):
        b = self._broker()
        b.trading.get_order_by_id.side_effect = self._api_error(404)
        self.assertIsNone(b.get_order("ord-1"))

    def test_401_raises(self):
        from alpaca.common.exceptions import APIError
        b = self._broker()
        b.trading.get_order_by_id.side_effect = self._api_error(401)
        with self.assertRaises(APIError):
            b.get_order("ord-1")

    def test_429_raises(self):
        from alpaca.common.exceptions import APIError
        b = self._broker()
        b.trading.get_order_by_id.side_effect = self._api_error(429)
        with self.assertRaises(APIError):
            b.get_order("ord-1")

    def test_network_error_raises(self):
        b = self._broker()
        b.trading.get_order_by_id.side_effect = ConnectionError("network down")
        with self.assertRaises(ConnectionError):
            b.get_order("ord-1")

    def test_timeout_raises(self):
        b = self._broker()
        b.trading.get_order_by_id.side_effect = TimeoutError("timed out")
        with self.assertRaises(TimeoutError):
            b.get_order("ord-1")

    # -- is_working -----------------------------------------------------

    def test_working_classification(self):
        from bot.broker import Broker
        for s in ("new", "accepted", "partially_filled", "pending_new", "held"):
            self.assertTrue(Broker.is_working(s), s)
        for s in ("filled", "canceled", "cancelled", "expired", "rejected"):
            self.assertFalse(Broker.is_working(s), s)

    def test_unknown_status_counts_as_working(self):
        from bot.broker import Broker
        self.assertTrue(Broker.is_working("something_new_from_alpaca"))

    # -- wait_for_fill --------------------------------------------------

    def test_wait_returns_on_fill(self):
        b = self._broker()
        b.trading.get_order_by_id.return_value = self._order("filled", 10, 110.0)
        out = b.wait_for_fill("ord-1", timeout=0.05, interval=0.01)
        self.assertEqual(out["status"], "filled")

    def test_wait_times_out_on_working_order(self):
        b = self._broker()
        b.trading.get_order_by_id.return_value = self._order("new")
        out = b.wait_for_fill("ord-1", timeout=0.05, interval=0.01)
        self.assertEqual(out["status"], "new")   # returned, but NOT filled

    def test_wait_raises_when_status_never_readable(self):
        b = self._broker()
        b.trading.get_order_by_id.side_effect = TimeoutError("timed out")
        with self.assertRaises(TimeoutError):
            b.wait_for_fill("ord-1", timeout=0.05, interval=0.01)

    def test_wait_recovers_from_a_transient_error(self):
        b = self._broker()
        b.trading.get_order_by_id.side_effect = [
            ConnectionError("blip"), self._order("filled", 10, 110.0)]
        out = b.wait_for_fill("ord-1", timeout=1.0, interval=0.01)
        self.assertEqual(out["status"], "filled")

    def test_wait_returns_none_on_404(self):
        b = self._broker()
        b.trading.get_order_by_id.side_effect = self._api_error(404)
        self.assertIsNone(b.wait_for_fill("ord-1", timeout=0.05, interval=0.01))


class TestExitLifecycle(unittest.TestCase):
    """A ledger trade closes only on a confirmed, fully filled close order."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db)
        storage.init_db(self.db)
        self.cfg = Settings(api_key="k", secret_key="s", db_path=self.db)
        self.trade_id = storage.open_trade("SPY", "long", 10, 100.0,
                                           "entry", path=self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(self.db + suffix).unlink(missing_ok=True)

    def _bot(self, wait_result=None, wait_raises=None, get_order=None):
        from bot.broker import Broker
        from bot.runner import TradingBot

        bot = TradingBot.__new__(TradingBot)
        bot.cfg = self.cfg
        bot._stop = False
        b = unittest.mock.MagicMock()
        # real status vocabulary, mocked transport
        b.FILLED, b.DEAD = Broker.FILLED, Broker.DEAD
        b.is_working = Broker.is_working
        b.submit_market_order.return_value = {"id": "ord-1", "status": "pending_new"}
        b.position.return_value = {"symbol": "SPY", "qty": 10.0, "side": "long",
                                   "avg_entry_price": 100.0, "current_price": 99.0,
                                   "market_value": 990.0, "unrealized_pl": -10.0,
                                   "unrealized_plpc": -1.0}
        b.cancel_open_orders.return_value = 0
        if wait_raises is not None:
            b.wait_for_fill.side_effect = wait_raises
        else:
            b.wait_for_fill.return_value = wait_result
        if get_order is not None:
            b.get_order_by_client_id.side_effect = (
                get_order if callable(get_order) else None)
            if not callable(get_order):
                b.get_order_by_client_id.return_value = get_order
        bot.broker = b
        return bot

    @staticmethod
    def _order(status, filled_qty=0, avg=None, oid="ord-1"):
        return {"id": oid, "status": status,
                "filled_qty": filled_qty, "filled_avg_price": avg}

    def _trade(self):
        return storage.get_open_trade(path=self.db)

    def _assert_still_open(self, result):
        t = self._trade()
        self.assertIsNotNone(t, "ledger trade was closed without a confirmed fill")
        self.assertEqual(t["status"], "open")
        self.assertIsNone(storage.get_state("last_exit", path=self.db),
                          "cooldown armed without a confirmed exit")
        self.assertIn(result["action"], ("exit-unconfirmed", "exit-pending", "error"))

    # -- the happy path --------------------------------------------------

    def test_fill_closes_the_trade(self):
        bot = self._bot(wait_result=self._order("filled", 10, 110.0))
        res = bot._exit(self._trade(), "trailing stop", 109.0)
        self.assertEqual(res["action"], "sell")
        self.assertAlmostEqual(res["fill_price"], 110.0)
        self.assertAlmostEqual(res["pnl"], 100.0)
        self.assertIsNone(self._trade())
        self.assertIsNone(storage.get_state("pending_exit", path=self.db))
        self.assertIsNotNone(storage.get_state("last_exit", path=self.db))

    def test_fill_price_is_used_not_the_signal_price(self):
        bot = self._bot(wait_result=self._order("filled", 10, 97.5))
        bot._exit(self._trade(), "stop loss", 109.0)   # signal price 109 is a decoy
        row = storage.recent_trades(1, path=self.db)[0]
        self.assertAlmostEqual(row["exit_price"], 97.5)
        self.assertAlmostEqual(row["pnl"], -25.0)

    def test_trade_closes_exactly_once(self):
        bot = self._bot(wait_result=self._order("filled", 10, 110.0))
        trade = self._trade()
        bot._exit(trade, "trailing stop", 109.0)
        first = storage.recent_trades(1, path=self.db)[0]
        # a duplicated exit against the stale row must not rewrite it
        bot._exit(trade, "duplicate", 200.0)
        rows = storage.recent_trades(path=self.db)
        self.assertEqual(len(rows), 1)
        self.assertAlmostEqual(rows[0]["pnl"], first["pnl"])
        self.assertEqual(rows[0]["exit_reason"], first["exit_reason"])

    # -- non-filled terminal and working states --------------------------

    def test_new_does_not_close(self):
        bot = self._bot(wait_result=self._order("new"))
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    def test_partially_filled_does_not_close(self):
        bot = self._bot(wait_result=self._order("partially_filled", 4, 99.5))
        res = bot._exit(self._trade(), "stop", 99.0)
        self._assert_still_open(res)
        self.assertIn("partial", res["reason"].lower())

    def test_canceled_does_not_close(self):
        bot = self._bot(wait_result=self._order("canceled"))
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    def test_expired_does_not_close(self):
        bot = self._bot(wait_result=self._order("expired"))
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    def test_rejected_does_not_close(self):
        bot = self._bot(wait_result=self._order("rejected"))
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    def test_filled_without_a_price_does_not_close(self):
        bot = self._bot(wait_result=self._order("filled", 10, None))
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    def test_vanished_order_does_not_close(self):
        bot = self._bot(wait_result=None)
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    # -- unreachable broker ----------------------------------------------

    def test_timeout_during_confirmation_does_not_close(self):
        bot = self._bot(wait_raises=TimeoutError("timed out"))
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    def test_auth_failure_during_confirmation_does_not_close(self):
        from alpaca.common.exceptions import APIError
        http = unittest.mock.MagicMock()
        http.response.status_code = 401
        bot = self._bot(wait_raises=APIError('{"message":"unauthorized."}', http))
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    def test_rate_limit_during_confirmation_does_not_close(self):
        from alpaca.common.exceptions import APIError
        http = unittest.mock.MagicMock()
        http.response.status_code = 429
        bot = self._bot(wait_raises=APIError('{"message":"too many."}', http))
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    def test_close_submission_failure_does_not_close(self):
        bot = self._bot(wait_result=self._order("filled", 10, 110.0))
        bot.broker.submit_market_order.side_effect = ConnectionError("network down")
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))

    def test_unreadable_position_does_not_submit_a_close(self):
        """Sizing a close against an unknown position is not allowed."""
        bot = self._bot(wait_result=self._order("filled", 10, 110.0))
        bot.broker.position.side_effect = TimeoutError("timed out")
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))
        bot.broker.submit_market_order.assert_not_called()

    def test_vanished_position_does_not_submit_a_close(self):
        bot = self._bot(wait_result=self._order("filled", 10, 110.0))
        bot.broker.position.return_value = None
        self._assert_still_open(bot._exit(self._trade(), "stop", 99.0))
        bot.broker.submit_market_order.assert_not_called()

    def test_close_is_sized_from_the_live_position(self):
        """The ledger quantity is not what gets sold — the broker's is."""
        bot = self._bot(wait_result=self._order("filled", 6, 110.0))
        bot.broker.position.return_value = dict(bot.broker.position.return_value,
                                                qty=6.0)
        bot._exit(self._trade(), "stop", 99.0)
        _, kwargs = bot.broker.submit_market_order.call_args
        args, _ = bot.broker.submit_market_order.call_args
        self.assertEqual(args[2], 6.0, "sold the ledger quantity, not the broker's")

    # -- no duplicate close orders ---------------------------------------

    def test_does_not_resubmit_while_an_exit_is_working(self):
        bot = self._bot(wait_result=self._order("new"))
        trade = self._trade()
        bot._exit(trade, "stop", 99.0)                 # submits ord-1, stays 'new'
        self.assertEqual(bot.broker.submit_market_order.call_count, 1)

        bot.broker.get_order_by_client_id.return_value = self._order("new")
        res = bot._exit(self._trade(), "stop", 98.0)   # next cycle
        self.assertEqual(res["action"], "exit-pending")
        self.assertEqual(bot.broker.submit_market_order.call_count, 1,
                         "a second close order was sent while one was working")
        self.assertIsNotNone(self._trade())

    def test_pending_exit_that_filled_later_is_booked(self):
        bot = self._bot(wait_result=self._order("new"))
        bot._exit(self._trade(), "trailing stop", 99.0)
        self.assertIsNotNone(self._trade())

        bot.broker.get_order_by_client_id.return_value = self._order("filled", 10, 96.0)
        res = bot._exit(self._trade(), "ignored", 99.0)
        self.assertEqual(res["action"], "sell")
        self.assertAlmostEqual(res["fill_price"], 96.0)
        self.assertIsNone(self._trade())
        # the original reason is preserved across the retry
        self.assertEqual(storage.recent_trades(1, path=self.db)[0]["exit_reason"],
                         "trailing stop")
        self.assertEqual(bot.broker.submit_market_order.call_count, 1)

    def test_dead_pending_exit_is_resubmitted(self):
        bot = self._bot(wait_result=self._order("new"))
        bot._exit(self._trade(), "stop", 99.0)
        self.assertEqual(bot.broker.submit_market_order.call_count, 1)

        bot.broker.get_order_by_client_id.return_value = self._order("canceled")
        bot.broker.wait_for_fill.return_value = self._order("filled", 10, 95.0)
        res = bot._exit(self._trade(), "stop", 99.0)
        self.assertEqual(res["action"], "sell")
        self.assertEqual(bot.broker.submit_market_order.call_count, 2,
                         "a dead close order should be replaced")

    def test_unreadable_pending_exit_does_not_resubmit(self):
        bot = self._bot(wait_result=self._order("new"))
        bot._exit(self._trade(), "stop", 99.0)
        bot.broker.get_order_by_client_id.side_effect = TimeoutError("timed out")
        res = bot._exit(self._trade(), "stop", 99.0)
        self._assert_still_open(res)
        self.assertEqual(bot.broker.submit_market_order.call_count, 1,
                         "resubmitted while the earlier order's fate was unknown")

    def test_dry_run_never_touches_the_ledger(self):
        cfg = Settings(api_key="k", secret_key="s", db_path=self.db, dry_run=True)
        bot = self._bot(wait_result=self._order("filled", 10, 110.0))
        bot.cfg = cfg
        res = bot._exit(self._trade(), "stop", 99.0)
        self.assertEqual(res["action"], "dry-run-sell")
        self.assertIsNotNone(self._trade())
        bot.broker.submit_market_order.assert_not_called()

    # -- ambiguous submission --------------------------------------------

    def test_client_order_id_is_persisted_before_submitting(self):
        """The id must survive a lost response, so it is written first."""
        bot = self._bot(wait_result=self._order("new"))
        seen = {}

        def capture(*args, **kwargs):
            seen["pending"] = storage.get_state("pending_exit", path=self.db)
            return {"id": "ord-1", "status": "pending_new"}

        bot.broker.submit_market_order.side_effect = capture
        bot._exit(self._trade(), "stop", 99.0)

        self.assertIsNotNone(seen["pending"], "nothing was persisted before the call")
        self.assertEqual(seen["pending"]["client_order_id"],
                         f"bot-exit-{self.trade_id}-1")
        _, kwargs = bot.broker.submit_market_order.call_args
        self.assertEqual(kwargs["client_order_id"], f"bot-exit-{self.trade_id}-1")

    def test_lost_response_is_resolved_by_client_id(self):
        """Alpaca accepted the close; the response never arrived."""
        bot = self._bot(wait_result=self._order("new"))
        bot.broker.submit_market_order.side_effect = ConnectionError("response lost")
        first = bot._exit(self._trade(), "stop", 99.0)
        self._assert_still_open(first)

        # next cycle: the order was there all along, and it filled
        bot.broker.get_order_by_client_id.return_value = self._order(
            "filled", 10, 97.0)
        second = bot._exit(self._trade(), "stop", 99.0)

        self.assertEqual(second["action"], "sell")
        self.assertAlmostEqual(second["fill_price"], 97.0)
        self.assertIsNone(self._trade())
        self.assertEqual(bot.broker.submit_market_order.call_count, 1,
                         "resubmitted despite the first close having filled")

    def test_submission_that_never_landed_reuses_the_same_id(self):
        bot = self._bot(wait_result=self._order("new"))
        bot.broker.submit_market_order.side_effect = ConnectionError("no route")
        bot._exit(self._trade(), "stop", 99.0)

        # Alpaca has no such order, so it never arrived
        bot.broker.get_order_by_client_id.return_value = None
        bot.broker.submit_market_order.side_effect = None
        bot.broker.submit_market_order.return_value = {"id": "ord-1",
                                                       "status": "pending_new"}
        bot._exit(self._trade(), "stop", 99.0)

        _, kwargs = bot.broker.submit_market_order.call_args
        self.assertEqual(kwargs["client_order_id"], f"bot-exit-{self.trade_id}-1",
                         "a fresh id was minted for an order that never existed")

    def test_unreadable_client_id_does_not_resubmit(self):
        """Not knowing whether the close exists must not mean sending another."""
        bot = self._bot(wait_result=self._order("new"))
        bot._exit(self._trade(), "stop", 99.0)
        bot.broker.get_order_by_client_id.side_effect = TimeoutError("timed out")
        res = bot._exit(self._trade(), "stop", 99.0)
        self._assert_still_open(res)
        self.assertEqual(bot.broker.submit_market_order.call_count, 1)

    def test_replacement_after_a_dead_order_uses_a_new_id(self):
        bot = self._bot(wait_result=self._order("new"))
        bot._exit(self._trade(), "stop", 99.0)

        bot.broker.get_order_by_client_id.return_value = self._order("canceled")
        bot.broker.wait_for_fill.return_value = self._order("filled", 10, 95.0)
        bot._exit(self._trade(), "stop", 99.0)

        _, kwargs = bot.broker.submit_market_order.call_args
        self.assertEqual(kwargs["client_order_id"], f"bot-exit-{self.trade_id}-2",
                         "a dead order's id was reused, which Alpaca rejects")

    def test_legacy_pending_exit_without_a_client_id_is_resolved(self):
        """A record written before exits carried client ids still blocks."""
        bot = self._bot(wait_result=self._order("new"))
        storage.set_state("pending_exit",
                          {"trade_id": self.trade_id, "order_id": "old-ord",
                           "reason": "stop"}, path=self.db)
        bot.broker.get_order.return_value = self._order("new", oid="old-ord")

        res = bot._exit(self._trade(), "stop", 99.0)

        self.assertEqual(res["action"], "exit-pending")
        bot.broker.get_order.assert_called_once_with("old-ord")
        bot.broker.submit_market_order.assert_not_called()


class TestPartialExitAccounting(unittest.TestCase):
    """Shares sold by an order that later dies still belong in the P&L."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db)
        storage.init_db(self.db)
        self.cfg = Settings(api_key="k", secret_key="s", db_path=self.db)
        self.trade_id = storage.open_trade("SPY", "long", 10, 700.0,
                                           "entry", path=self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(self.db + suffix).unlink(missing_ok=True)

    def _bot(self, position_qty=10.0):
        from bot.broker import Broker
        from bot.runner import TradingBot

        bot = TradingBot.__new__(TradingBot)
        bot.cfg = self.cfg
        bot._stop = False
        b = unittest.mock.MagicMock()
        b.FILLED, b.DEAD = Broker.FILLED, Broker.DEAD
        b.is_working = Broker.is_working
        b.cancel_open_orders.return_value = 0
        b.position.return_value = self._position(position_qty)
        bot.broker = b
        return bot

    @staticmethod
    def _position(qty):
        return {"symbol": "SPY", "qty": qty, "side": "long",
                "avg_entry_price": 700.0, "current_price": 750.0,
                "market_value": qty * 750.0, "unrealized_pl": 0.0,
                "unrealized_plpc": 0.0}

    @staticmethod
    def _order(status, filled_qty=0, avg=None, oid="ord-1"):
        return {"id": oid, "status": status,
                "filled_qty": filled_qty, "filled_avg_price": avg}

    def _trade(self):
        return storage.get_open_trade(path=self.db)

    def test_partial_fill_survives_a_dead_order(self):
        """10 @ 700 exits as 4 @ 750 then 6 @ 752: P&L is 512, not 520."""
        bot = self._bot()
        bot.broker.submit_market_order.side_effect = [
            {"id": "ord-1", "status": "pending_new"},
            {"id": "ord-2", "status": "pending_new"},
        ]

        # cycle 1: the close order fills 4 of 10 and stays working
        bot.broker.wait_for_fill.return_value = self._order(
            "partially_filled", 4, 750.0)
        first = bot._exit(self._trade(), "stop loss", 749.0)
        self.assertEqual(first["action"], "exit-unconfirmed")
        self.assertIsNotNone(self._trade())

        # cycle 2: that order is canceled with its 4 shares gone, so the
        # broker is down to 6, and the replacement fills those
        bot.broker.get_order_by_client_id.return_value = self._order("canceled", 4, 750.0)
        bot.broker.position.return_value = self._position(6.0)
        bot.broker.wait_for_fill.return_value = self._order(
            "filled", 6, 752.0, oid="ord-2")
        second = bot._exit(self._trade(), "stop loss", 751.0)

        self.assertEqual(second["action"], "sell")
        self.assertEqual(bot.broker.submit_market_order.call_count, 2)
        # the replacement covers the remaining 6, not the original 10
        self.assertEqual(bot.broker.submit_market_order.call_args[0][2], 6.0)
        self.assertIsNone(self._trade())

        row = storage.recent_trades(1, path=self.db)[0]
        # (750-700)*4 + (752-700)*6 == 512, not (752-700)*10 == 520
        self.assertAlmostEqual(row["pnl"], 512.0)
        self.assertAlmostEqual(row["exit_price"], 751.2)
        self.assertAlmostEqual(row["exit_qty"], 10.0)
        self.assertIsNone(storage.get_state("exit_fills", path=self.db))

    def test_partial_fill_is_recorded_once_across_retries(self):
        bot = self._bot()
        bot.broker.submit_market_order.return_value = {"id": "ord-1",
                                                      "status": "pending_new"}
        bot.broker.wait_for_fill.return_value = self._order("new")
        bot._exit(self._trade(), "stop", 99.0)

        dead = self._order("canceled", 4, 750.0)
        bot._record_partial_fill(self.trade_id, dead)
        bot._record_partial_fill(self.trade_id, dead)
        fills = bot._exit_fills(self.trade_id)
        self.assertEqual(len(fills), 1)
        self.assertAlmostEqual(fills[0]["qty"], 4.0)

    def test_fills_from_another_trade_are_ignored(self):
        bot = self._bot()
        storage.set_state("exit_fills",
                          {"trade_id": self.trade_id + 99,
                           "fills": [{"order_id": "x", "qty": 4, "price": 750.0}]},
                          path=self.db)
        self.assertEqual(bot._exit_fills(self.trade_id), [])

    def test_unweighable_final_fill_does_not_close(self):
        """A filled order with no quantity cannot be averaged against a partial."""
        bot = self._bot()
        storage.set_state("exit_fills",
                          {"trade_id": self.trade_id,
                           "fills": [{"order_id": "ord-1", "qty": 4, "price": 750.0}]},
                          path=self.db)
        res = bot._book_exit(self._trade(), self._order("filled", 0, 752.0), "stop")
        self.assertEqual(res["action"], "exit-unconfirmed")
        self.assertIsNotNone(self._trade())

    def test_partials_count_when_the_position_vanishes(self):
        bot = self._bot()
        storage.set_state("exit_fills",
                          {"trade_id": self.trade_id,
                           "fills": [{"order_id": "ord-1", "qty": 4, "price": 750.0}]},
                          path=self.db)
        trade, note = bot.reconcile(None, self._trade(), 760.0)
        self.assertIsNone(trade)
        row = storage.recent_trades(1, path=self.db)[0]
        # 4 @ 750 confirmed, the remaining 6 guessed at the last price
        self.assertAlmostEqual(row["pnl"], (750.0 - 700.0) * 4 + (760.0 - 700.0) * 6)
        self.assertAlmostEqual(row["exit_qty"], 10.0)


class TestPositionReconciliation(unittest.TestCase):
    """The ledger and the broker must agree before the bot manages anything."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.remove(self.db)
        storage.init_db(self.db)
        self.cfg = Settings(api_key="k", secret_key="s", db_path=self.db)
        self.trade_id = storage.open_trade("SPY", "long", 10, 700.0,
                                           "entry", path=self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(self.db + suffix).unlink(missing_ok=True)

    def _bot(self):
        from bot.runner import TradingBot

        bot = TradingBot.__new__(TradingBot)
        bot.cfg = self.cfg
        bot._stop = False
        bot.broker = unittest.mock.MagicMock()
        return bot

    def _pos(self, qty, side="long"):
        return {"symbol": "SPY", "qty": qty, "side": side,
                "avg_entry_price": 700.0, "current_price": 750.0,
                "market_value": qty * 750.0, "unrealized_pl": 0.0,
                "unrealized_plpc": 0.0}

    def _trade(self):
        return storage.get_open_trade(path=self.db)

    def test_matching_quantities_are_managed(self):
        bot = self._bot()
        trade, note = bot.reconcile(self._pos(10), self._trade(), 750.0)
        self.assertIsNotNone(trade)
        self.assertIsNone(note)

    def test_short_broker_side_is_not_managed_as_a_long(self):
        bot = self._bot()
        trade, note = bot.reconcile(self._pos(5, side="short"), self._trade(), 750.0)
        self.assertIsNone(trade, "a short position was handed to long-only logic")
        self.assertIn("long-only", note)
        self.assertIsNotNone(self._trade(), "the trade was closed on a side mismatch")

    def test_extra_broker_shares_block_trading(self):
        bot = self._bot()
        trade, note = bot.reconcile(self._pos(15), self._trade(), 750.0)
        self.assertIsNone(trade, "the close would have sold 5 untracked shares")
        self.assertIn("does not own", note)

    def test_missing_broker_shares_block_trading(self):
        bot = self._bot()
        trade, note = bot.reconcile(self._pos(6), self._trade(), 750.0)
        self.assertIsNone(trade)
        self.assertIn("sold behind the bot", note)

    def test_a_working_close_explains_a_smaller_position(self):
        bot = self._bot()
        storage.set_state("pending_exit",
                          {"trade_id": self.trade_id, "order_id": "ord-1"},
                          path=self.db)
        trade, note = bot.reconcile(self._pos(6), self._trade(), 750.0)
        self.assertIsNotNone(trade, "a close order in flight was treated as a mismatch")
        self.assertIsNone(note)

    def test_recorded_partials_explain_a_smaller_position(self):
        bot = self._bot()
        storage.set_state("exit_fills",
                          {"trade_id": self.trade_id,
                           "fills": [{"order_id": "ord-1", "qty": 4, "price": 750.0}]},
                          path=self.db)
        trade, note = bot.reconcile(self._pos(6), self._trade(), 750.0)
        self.assertIsNotNone(trade)
        self.assertIsNone(note)


class TestDatabaseCompatibility(unittest.TestCase):
    """An existing trading_bot.db must open and keep working."""

    OLD_SCHEMA = """
    CREATE TABLE trades (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        symbol        TEXT    NOT NULL,
        side          TEXT    NOT NULL,
        qty           REAL    NOT NULL,
        entry_time    TEXT    NOT NULL,
        entry_price   REAL    NOT NULL,
        entry_reason  TEXT,
        exit_time     TEXT,
        exit_price    REAL,
        exit_reason   TEXT,
        pnl           REAL,
        pnl_pct       REAL,
        high_water    REAL,
        status        TEXT    NOT NULL DEFAULT 'open'
    );
    """

    def setUp(self):
        import sqlite3
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        conn = sqlite3.connect(self.db)
        conn.executescript(self.OLD_SCHEMA)
        conn.execute(
            "INSERT INTO trades(symbol, side, qty, entry_time, entry_price, "
            "status) VALUES('SPY','long',10,'2026-01-02T14:30:00+00:00',700.0,'open')"
        )
        conn.commit()
        conn.close()

    def tearDown(self):
        for suffix in ("", "-wal", "-shm"):
            Path(self.db + suffix).unlink(missing_ok=True)

    def test_old_database_is_migrated_in_place(self):
        storage.init_db(self.db)
        trade = storage.get_open_trade(path=self.db)
        self.assertIsNotNone(trade)
        self.assertIn("exit_qty", trade)

        closed = storage.close_trade(trade["id"], 752.0, "stop",
                                     path=self.db, exit_qty=10.0)
        self.assertAlmostEqual(closed["pnl"], 520.0)
        self.assertAlmostEqual(closed["exit_qty"], 10.0)

    def test_migration_is_idempotent(self):
        storage.init_db(self.db)
        storage.init_db(self.db)
        self.assertIsNotNone(storage.get_open_trade(path=self.db))

    def test_unparseable_state_does_not_crash(self):
        import sqlite3
        storage.init_db(self.db)
        conn = sqlite3.connect(self.db)
        conn.execute("INSERT INTO bot_state(key, value) VALUES('pending_exit', ?)",
                     ("{not json",))
        conn.commit()
        conn.close()
        self.assertIsNone(storage.get_state("pending_exit", path=self.db))


class TestSingleInstance(unittest.TestCase):
    """Two runners against one account can submit duplicate orders."""

    def setUp(self):
        fd, self.db = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.cfg = Settings(api_key="k", secret_key="s", db_path=self.db)

    def tearDown(self):
        for suffix in ("", "-wal", "-shm", ".lock"):
            Path(self.db + suffix).unlink(missing_ok=True)

    def test_second_holder_is_refused(self):
        from bot.runner import single_instance

        with single_instance(self.cfg):
            with self.assertRaises(RuntimeError):
                with single_instance(self.cfg):
                    pass

    def test_lock_is_released_on_exit(self):
        from bot.runner import single_instance

        with single_instance(self.cfg):
            pass
        with single_instance(self.cfg):
            pass

    def test_lock_is_released_after_a_crash(self):
        from bot.runner import single_instance

        with self.assertRaises(ValueError):
            with single_instance(self.cfg):
                raise ValueError("cycle blew up")
        with single_instance(self.cfg):
            pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
