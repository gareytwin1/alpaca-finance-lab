"""Thin wrapper over alpaca-py.

Everything the bot and dashboard need from Alpaca lives here, so the rest
of the codebase never imports the SDK directly and stays easy to test.
"""

from __future__ import annotations

import logging
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

    def submit_market_order(self, symbol: str, side: str, qty: float) -> dict:
        req = MarketOrderRequest(
            symbol=symbol,
            qty=qty,
            side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
            time_in_force=TimeInForce.DAY,
        )
        order = self.trading.submit_order(order_data=req)
        log.info("Submitted %s %s x%s -> order %s", side, symbol, qty, order.id)
        return {
            "id": str(order.id),
            "symbol": order.symbol,
            "side": side,
            "qty": float(order.qty or qty),
            "status": str(getattr(order.status, "value", order.status)),
        }

    def close_position(self, symbol: str) -> dict | None:
        try:
            order = self.trading.close_position(symbol)
        except Exception as exc:
            log.error("Failed to close %s: %s", symbol, exc)
            return None
        return {"id": str(order.id), "status": str(getattr(order.status, "value", order.status))}

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

    def get_order(self, order_id: str) -> dict | None:
        try:
            o = self.trading.get_order_by_id(order_id)
        except Exception as exc:
            log.warning("Could not read order %s: %s", order_id, exc)
            return None
        return {
            "id": str(o.id),
            "status": str(getattr(o.status, "value", o.status)),
            "filled_qty": float(o.filled_qty or 0),
            "filled_avg_price": float(o.filled_avg_price or 0) or None,
        }

    def wait_for_fill(self, order_id: str, timeout: float = 20.0,
                      interval: float = 1.0) -> dict | None:
        """Polls until the order reaches a terminal state or `timeout` passes."""
        import time

        terminal = {"filled", "canceled", "cancelled", "expired", "rejected"}
        deadline = time.monotonic() + timeout
        order = self.get_order(order_id)
        while time.monotonic() < deadline:
            if order and order["status"].lower() in terminal:
                return order
            time.sleep(interval)
            order = self.get_order(order_id)
        return order
