"""Thin wrapper over alpaca-py.

Everything the bot and dashboard need from Alpaca lives here, so the rest
of the codebase never imports the SDK directly and stays easy to test.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone

import pandas as pd
from alpaca.common.exceptions import APIError
from alpaca.data.enums import DataFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, StockLatestQuoteRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest

from .settings import Settings, settings as default_settings

log = logging.getLogger(__name__)


class Broker:
    """Paper-trading facade. Refuses to construct against a live endpoint."""

    def __init__(self, cfg: Settings = default_settings):
        problems = cfg.validate()
        if problems:
            raise ValueError("Invalid configuration: " + "; ".join(problems))
        self.cfg = cfg
        self.trading = TradingClient(cfg.api_key, cfg.secret_key, paper=cfg.is_paper)
        self.data = StockHistoricalDataClient(cfg.api_key, cfg.secret_key)
        self._feed = DataFeed(cfg.data_feed.lower())

    # -- account ---------------------------------------------------------

    def account(self) -> dict:
        acct = self.trading.get_account()
        equity = float(acct.equity or 0)
        last_equity = float(acct.last_equity or 0)
        return {
            "status": str(getattr(acct.status, "value", acct.status)),
            "equity": equity,
            "last_equity": last_equity,
            "cash": float(acct.cash or 0),
            "buying_power": float(acct.buying_power or 0),
            "day_pnl": equity - last_equity,
            "day_pnl_pct": ((equity - last_equity) / last_equity * 100) if last_equity else 0.0,
            "pattern_day_trader": bool(getattr(acct, "pattern_day_trader", False)),
        }

    def clock(self) -> dict:
        c = self.trading.get_clock()
        return {
            "is_open": bool(c.is_open),
            "timestamp": c.timestamp.isoformat(),
            "next_open": c.next_open.isoformat(),
            "next_close": c.next_close.isoformat(),
        }

    def minutes_to_close(self, clock: dict | None = None) -> float | None:
        """Minutes until the bell, or None when the market is closed.

        Pass an already-fetched clock to avoid a redundant API call; each
        extra call is rate-limit budget spent for no new information.
        """
        if clock is None:
            c = self.trading.get_clock()
            is_open, ts, close = c.is_open, c.timestamp, c.next_close
        else:
            is_open = clock["is_open"]
            ts = datetime.fromisoformat(clock["timestamp"])
            close = datetime.fromisoformat(clock["next_close"])
        if not is_open:
            return None
        return (close - ts).total_seconds() / 60

    # -- positions & orders ----------------------------------------------

    def position(self, symbol: str) -> dict | None:
        """The live broker position, or None when genuinely flat.

        Only a 404 means "no position". Every other failure — 401, 429, a
        timeout — is re-raised so the caller skips the cycle instead of
        mistaking an unreachable broker for a closed position.
        """
        try:
            p = self.trading.get_open_position(symbol)
        except APIError as exc:
            if getattr(exc, "status_code", None) == 404:
                return None
            log.error("Position lookup for %s failed (%s); treating the "
                      "position as unknown.", symbol, exc)
            raise
        except Exception as exc:
            log.error("Position lookup for %s failed (%s); treating the "
                      "position as unknown.", symbol, exc)
            raise
        return {
            "symbol": p.symbol,
            "qty": float(p.qty),
            "side": str(getattr(p.side, "value", p.side)),
            "avg_entry_price": float(p.avg_entry_price),
            "current_price": float(p.current_price or 0),
            "market_value": float(p.market_value or 0),
            "unrealized_pl": float(p.unrealized_pl or 0),
            "unrealized_plpc": float(p.unrealized_plpc or 0) * 100,
        }

    def open_orders(self, symbol: str | None = None) -> list[dict]:
        req = GetOrdersRequest(
            status=QueryOrderStatus.OPEN,
            symbols=[symbol] if symbol else None,
        )
        return [
            {
                "id": str(o.id),
                "symbol": o.symbol,
                "side": str(getattr(o.side, "value", o.side)),
                "qty": float(o.qty or 0),
                "type": str(getattr(o.order_type, "value", o.order_type)),
                "status": str(getattr(o.status, "value", o.status)),
                "submitted_at": o.submitted_at.isoformat() if o.submitted_at else None,
            }
            for o in self.trading.get_orders(filter=req)
        ]

    def submit_market_order(self, symbol: str, side: str, qty: float,
                            client_order_id: str | None = None) -> dict:
        """Submits a market order. Raises if the submission is not accepted.

        `client_order_id` is an idempotency key the caller owns. Alpaca stores
        it on the order, so a caller that persisted the id before calling this
        can find the order again with `get_order_by_client_id` even when the
        response to this request is lost.
        """
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
            client_order_id=client_order_id,
        )
        order = self.trading.submit_order(order_data=req)
        log.info("Submitted %s %s x%s (%s) -> order %s",
                 side, symbol, qty, client_order_id or "no client id", order.id)
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "side": side,
            "qty": float(order.qty or qty),
            "status": str(getattr(order.status, "value", order.status)),
        }

    def cancel_open_orders(self, symbol: str) -> int:
        cancelled = 0
        for o in self.open_orders(symbol):
            try:
                self.trading.cancel_order_by_id(o["id"])
                cancelled += 1
            except Exception as exc:
                log.warning("Could not cancel order %s: %s", o["id"], exc)
        return cancelled

    # -- market data -------------------------------------------------------

    def bars(self, symbol: str, minutes: int, limit: int) -> pd.DataFrame:
        """Recent minute bars, oldest first, indexed by timestamp (UTC).

        The still-forming current bar is dropped so signals only ever fire
        on completed bars.
        """
        end = datetime.now(timezone.utc)
        # Generous window: overnight gaps and weekends mean wall-clock
        # minutes are a poor proxy for the number of bars available.
        span = max(limit * minutes * 4, 60 * 24)
        start = end - timedelta(minutes=span)

        req = StockBarsRequest(
            symbol_or_symbols=[symbol],
            start=start,
            end=end,
            timeframe=TimeFrame(minutes, TimeFrameUnit.Minute),
            feed=self._feed,
        )
        bars = self.data.get_stock_bars(req)
        df = bars.df
        if df is None or df.empty:
            return pd.DataFrame()

        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(symbol, level="symbol")
        df = df.sort_index()

        # Drop the in-progress bar at the right edge.
        bar_end = df.index[-1] + pd.Timedelta(minutes=minutes)
        if bar_end > pd.Timestamp(end):
            df = df.iloc[:-1]

        return df.tail(limit)

    def latest_price(self, symbol: str) -> float | None:
        """Mid price from the latest quote, falling back to the last bar."""
        try:
            quotes = self.data.get_stock_latest_quote(
                StockLatestQuoteRequest(symbol_or_symbols=symbol, feed=self._feed)
            )
            q = quotes[symbol]
            bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2
            return ask or bid or None
        except Exception as exc:
            log.warning("Quote lookup failed for %s: %s", symbol, exc)
            df = self.bars(symbol, self.cfg.bar_minutes, 1)
            return float(df["close"].iloc[-1]) if not df.empty else None

    # -- fills -------------------------------------------------------------

    #: Order states that mean the order is done and fully executed.
    FILLED = frozenset({"filled"})
    #: Terminal states where no shares will trade from this order.
    DEAD = frozenset({"canceled", "cancelled", "expired", "rejected",
                      "done_for_day", "suspended"})

    @classmethod
    def is_working(cls, status: str) -> bool:
        """True while an order may still execute (new, accepted, partial...).

        Anything not known to be filled or dead counts as working, so an
        unfamiliar status never licenses a second order.
        """
        s = status.lower()
        return s not in cls.FILLED and s not in cls.DEAD

    @staticmethod
    def _order_dict(o) -> dict:
        return {
            "id": str(o.id),
            "client_order_id": str(getattr(o, "client_order_id", "") or "") or None,
            "status": str(getattr(o.status, "value", o.status)),
            "filled_qty": float(o.filled_qty or 0),
            "filled_avg_price": float(o.filled_avg_price or 0) or None,
        }

    def get_order(self, order_id: str) -> dict | None:
        """The order, or None only when Alpaca says it does not exist (404).

        Every other failure — 401, 429, 5xx, a timeout — is re-raised. An
        unreadable order is not an absent order, and the caller must not be
        able to confuse the two.
        """
        try:
            o = self.trading.get_order_by_id(order_id)
        except APIError as exc:
            if getattr(exc, "status_code", None) == 404:
                log.warning("Order %s does not exist (404).", order_id)
                return None
            log.error("Order %s status unreadable (%s).", order_id, exc)
            raise
        except Exception as exc:
            log.error("Order %s status unreadable (%s).", order_id, exc)
            raise
        return self._order_dict(o)

    def get_order_by_client_id(self, client_order_id: str) -> dict | None:
        """The order carrying this client id, or None if Alpaca has none.

        None is the answer to "did my submission land?" — and only a 404 may
        produce it. Anything else is re-raised, because an unreadable broker
        must never be mistaken for a submission that never arrived.
        """
        try:
            o = self.trading.get_order_by_client_id(client_order_id)
        except APIError as exc:
            if getattr(exc, "status_code", None) == 404:
                log.info("No order exists for client id %s.", client_order_id)
                return None
            log.error("Client id %s unreadable (%s).", client_order_id, exc)
            raise
        except Exception as exc:
            log.error("Client id %s unreadable (%s).", client_order_id, exc)
            raise
        return self._order_dict(o)

    def wait_for_fill(self, order_id: str, timeout: float = 20.0,
                      interval: float = 1.0) -> dict | None:
        """Polls until the order settles or `timeout` elapses.

        Returns the last successfully read order — which on timeout may still
        be working or partially filled, so callers must check `status`
        themselves rather than assuming a fill.

        Returns None only for a 404. Transient read errors are retried until
        the deadline; if the most recent read still failed, the exception is
        raised so an unreachable broker is never reported as an outcome.
        """
        deadline = time.monotonic() + timeout
        order: dict | None = None
        last_exc: Exception | None = None

        while True:
            try:
                order = self.get_order(order_id)
                last_exc = None
                if order is None:
                    return None
                if not self.is_working(order["status"]):
                    return order
            except Exception as exc:
                last_exc = exc
                log.warning("Retrying status read for order %s: %s", order_id, exc)

            if time.monotonic() >= deadline:
                break
            time.sleep(interval)

        if last_exc is not None:
            raise last_exc
        return order
