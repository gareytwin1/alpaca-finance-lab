"""The trading loop: guards, signals, orders and bookkeeping.

Run it with::

    python -m bot.runner            # live paper loop
    python -m bot.runner --once     # a single cycle, then exit
    python -m bot.runner --dry-run  # evaluate signals, never send orders
    python -m bot.runner --status   # print state and exit
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from datetime import datetime, timezone

from . import storage
from .broker import Broker
from .settings import Settings, settings as default_settings
from .strategy import add_indicators, entry_signal, exit_signal, position_size

log = logging.getLogger("bot")


def setup_logging(cfg: Settings, verbose: bool = False) -> None:
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=[logging.FileHandler(cfg.log_path), logging.StreamHandler(sys.stdout)],
    )
    logging.getLogger("alpaca").setLevel(logging.WARNING)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


class TradingBot:
    def __init__(self, cfg: Settings = default_settings, broker: Broker | None = None):
        self.cfg = cfg
        self.broker = broker or Broker(cfg)
        storage.init_db(cfg.db_path)
        self._stop = False

    # -- helpers ---------------------------------------------------------

    def _state(self, key: str, value) -> None:
        storage.set_state(key, value, path=self.cfg.db_path)

    def _note(self, level: str, message: str) -> None:
        getattr(log, level.lower(), log.info)(message)
        storage.log_event(level, message, path=self.cfg.db_path)

    def _cooldown_remaining(self) -> int:
        last_exit = storage.get_state("last_exit", path=self.cfg.db_path)
        if not last_exit:
            return 0
        try:
            exited = datetime.fromisoformat(last_exit)
        except ValueError:
            return 0
        if exited.tzinfo is None:
            exited = exited.replace(tzinfo=timezone.utc)
        elapsed = (datetime.now(timezone.utc) - exited).total_seconds()
        return max(0, int(self.cfg.cooldown_seconds - elapsed))

    def _trades_today(self) -> int:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        return storage.count_trades_on(today, path=self.cfg.db_path)

    # -- reconciliation --------------------------------------------------

    def reconcile(self, broker_pos: dict | None, db_trade: dict | None,
                  price: float | None) -> tuple[dict | None, str | None]:
        """Aligns our books with the broker's.

        Returns the trade we may manage plus a note about anything odd.
        A position the bot did not open is never touched.
        """
        if db_trade and not broker_pos:
            # Someone (or a stop) closed it behind our back.
            exit_price = price or db_trade["entry_price"]
            storage.close_trade(db_trade["id"], exit_price,
                                "closed outside the bot", path=self.cfg.db_path)
            self._state("last_exit", storage.utcnow())
            self._note("warning",
                       f"Trade #{db_trade['id']} was closed outside the bot; "
                       f"booked at {exit_price:.2f}.")
            return None, "orphaned trade reconciled"

        if broker_pos and not db_trade:
            note = (f"Untracked {broker_pos['side']} position of "
                    f"{broker_pos['qty']:g} {broker_pos['symbol']} exists "
                    f"(entry {broker_pos['avg_entry_price']:.2f}). The bot will "
                    f"not trade this symbol until it is closed or adopted "
                    f"(--adopt).")
            self._state("untracked_position", note)
            return None, note

        self._state("untracked_position", None)
        return db_trade, None

    def adopt_position(self) -> dict | None:
        """Takes an existing broker position under bot management."""
        try:
            pos = self.broker.position(self.cfg.symbol)
        except Exception as exc:
            self._note("error", f"Cannot adopt: position lookup failed ({exc}).")
            return None
        if not pos:
            self._note("info", "Nothing to adopt: no open position.")
            return None
        if storage.get_open_trade(path=self.cfg.db_path):
            self._note("info", "A tracked trade is already open; nothing to adopt.")
            return None
        trade_id = storage.open_trade(
            symbol=pos["symbol"], side=pos["side"], qty=pos["qty"],
            entry_price=pos["avg_entry_price"], entry_reason="adopted existing position",
            path=self.cfg.db_path,
        )
        storage.update_high_water(trade_id, max(pos["avg_entry_price"], pos["current_price"]),
                                  path=self.cfg.db_path)
        self._state("untracked_position", None)
        self._note("info", f"Adopted position as trade #{trade_id}.")
        return storage.get_open_trade(path=self.cfg.db_path)

    # -- guards ----------------------------------------------------------

    def entry_guards(self, account: dict, clock: dict) -> str | None:
        """Returns the reason entries are blocked, or None when clear."""
        if not clock["is_open"]:
            return "market closed"

        if self.cfg.flat_before_close_minutes:
            left = self.broker.minutes_to_close(clock)
            if left is not None and left <= self.cfg.flat_before_close_minutes:
                return f"within {self.cfg.flat_before_close_minutes} min of the close"

        cooldown = self._cooldown_remaining()
        if cooldown:
            return f"cooldown active ({cooldown}s left)"

        trades = self._trades_today()
        if trades >= self.cfg.max_trades_per_day:
            return f"daily trade cap reached ({trades}/{self.cfg.max_trades_per_day})"

        if account["day_pnl"] <= -abs(self.cfg.daily_loss_limit):
            return (f"daily loss limit hit "
                    f"({account['day_pnl']:.2f} <= -{self.cfg.daily_loss_limit:.2f})")

        if self.broker.open_orders(self.cfg.symbol):
            return "an order is still working"

        return None

    # -- the cycle -------------------------------------------------------

    def run_once(self) -> dict:
        cfg = self.cfg
        cycle: dict = {"ts": storage.utcnow(), "symbol": cfg.symbol}

        account = self.broker.account()
        clock = self.broker.clock()
        storage.record_equity(account["equity"], account["cash"], account["day_pnl"],
                              path=cfg.db_path)
        cycle["account"] = account
        cycle["market_open"] = clock["is_open"]

        bars = self.broker.bars(cfg.symbol, cfg.bar_minutes, cfg.lookback_bars)
        if bars.empty or len(bars) < cfg.ema_slow + 2:
            cycle["action"] = "skip"
            cycle["reason"] = f"only {len(bars)} bars available"
            self._state("last_cycle", cycle)
            return cycle

        df = add_indicators(bars, cfg)
        price = float(df["close"].iloc[-1])
        cycle["price"] = price
        cycle["bar_time"] = str(df.index[-1])

        broker_pos = self.broker.position(cfg.symbol)
        db_trade = storage.get_open_trade(path=cfg.db_path)
        db_trade, note = self.reconcile(broker_pos, db_trade, price)
        if note:
            cycle["note"] = note

        # ---- managing an open position --------------------------------
        if db_trade and broker_pos:
            high = max(db_trade.get("high_water") or 0, float(df["high"].iloc[-1]), price)
            storage.update_high_water(db_trade["id"], high, path=cfg.db_path)

            sig = exit_signal(df, db_trade["entry_price"], high, cfg)

            minutes_left = self.broker.minutes_to_close(clock)
            if (sig.action == "hold" and cfg.flat_before_close_minutes
                    and minutes_left is not None
                    and minutes_left <= cfg.flat_before_close_minutes):
                sig.action, sig.reason = "sell", "flattening into the close"

            cycle["position"] = broker_pos
            cycle["signal"] = {"action": sig.action, "reason": sig.reason,
                               "indicators": sig.indicators}

            if sig.action == "sell":
                cycle.update(self._exit(db_trade, sig.reason, price))
            else:
                cycle["action"] = "hold"
                cycle["reason"] = sig.reason
            self._state("last_cycle", cycle)
            return cycle

        # ---- looking for an entry --------------------------------------
        blocked = note or self.entry_guards(account, clock)
        sig = entry_signal(df, cfg)
        cycle["signal"] = {"action": sig.action, "reason": sig.reason,
                           "indicators": sig.indicators}

        if blocked:
            cycle["action"] = "blocked"
            cycle["reason"] = blocked
        elif sig.action == "buy":
            cycle.update(self._enter(account, sig.reason, price))
        else:
            cycle["action"] = "wait"
            cycle["reason"] = sig.reason

        self._state("last_cycle", cycle)
        return cycle

    # -- order execution -------------------------------------------------

    def _enter(self, account: dict, reason: str, price: float) -> dict:
        cfg = self.cfg
        qty = position_size(account["buying_power"], price, cfg)
        if qty < 1:
            return {"action": "skip", "reason": "position size rounds to zero shares"}

        if cfg.dry_run:
            self._note("info", f"[DRY RUN] would BUY {qty} {cfg.symbol} @ ~{price:.2f} ({reason})")
            return {"action": "dry-run-buy", "reason": reason, "qty": qty}

        try:
            order = self.broker.submit_market_order(cfg.symbol, "buy", qty)
        except Exception as exc:
            self._note("error", f"Buy order rejected: {exc}")
            return {"action": "error", "reason": str(exc)}

        fill = self.broker.wait_for_fill(order["id"])
        if not fill or fill["status"].lower() != "filled":
            status = fill["status"] if fill else "unknown"
            self._note("warning", f"Buy order {order['id']} did not fill (status {status}).")
            return {"action": "unfilled", "reason": f"order status {status}"}

        entry_price = fill["filled_avg_price"] or price
        trade_id = storage.open_trade(cfg.symbol, "long", fill["filled_qty"],
                                      entry_price, reason, path=cfg.db_path)
        self._note("info",
                   f"ENTER long #{trade_id}: {fill['filled_qty']:g} {cfg.symbol} "
                   f"@ {entry_price:.2f} — {reason}")
        return {"action": "buy", "reason": reason, "qty": fill["filled_qty"],
                "fill_price": entry_price, "trade_id": trade_id}

    def _exit(self, db_trade: dict, reason: str, price: float) -> dict:
        cfg = self.cfg
        if cfg.dry_run:
            self._note("info", f"[DRY RUN] would SELL trade #{db_trade['id']} @ ~{price:.2f} ({reason})")
            return {"action": "dry-run-sell", "reason": reason}

        self.broker.cancel_open_orders(cfg.symbol)
        result = self.broker.close_position(cfg.symbol)
        if not result:
            self._note("error", f"Could not close trade #{db_trade['id']}; will retry next cycle.")
            return {"action": "error", "reason": "close_position failed"}

        fill = self.broker.wait_for_fill(result["id"])
        exit_price = (fill or {}).get("filled_avg_price") or price
        closed = storage.close_trade(db_trade["id"], exit_price, reason, path=cfg.db_path)
        self._state("last_exit", storage.utcnow())
        pnl = (closed or {}).get("pnl", 0.0) or 0.0
        self._note("info",
                   f"EXIT #{db_trade['id']} @ {exit_price:.2f} — {reason} | "
                   f"P&L {pnl:+.2f}")
        return {"action": "sell", "reason": reason, "fill_price": exit_price,
                "pnl": pnl, "trade_id": db_trade["id"]}

    # -- loop -------------------------------------------------------------

    def run_forever(self) -> None:
        cfg = self.cfg
        self._note("info",
                   f"Bot started on {cfg.symbol} ({cfg.bar_minutes}m bars, "
                   f"ema{cfg.ema_fast}/{cfg.ema_slow}, "
                   f"{'DRY RUN' if cfg.dry_run else 'paper trading'}).")

        def _handle(signum, _frame):
            self._stop = True
            self._note("info", f"Signal {signum} received; finishing this cycle.")

        signal.signal(signal.SIGINT, _handle)
        signal.signal(signal.SIGTERM, _handle)

        while not self._stop:
            started = time.monotonic()
            try:
                cycle = self.run_once()
                self._state("heartbeat", storage.utcnow())
                log.info("cycle: %s — %s", cycle.get("action"), cycle.get("reason"))
            except Exception as exc:
                self._note("error", f"Cycle failed: {exc}")
                log.debug("traceback", exc_info=True)

            elapsed = time.monotonic() - started
            for _ in range(int(max(1, cfg.poll_seconds - elapsed))):
                if self._stop:
                    break
                time.sleep(1)

        self._note("info", "Bot stopped.")


# --------------------------------------------------------------------------

def _print_status(bot: TradingBot) -> None:
    cfg = bot.cfg
    account = bot.broker.account()
    clock = bot.broker.clock()
    try:
        pos = bot.broker.position(cfg.symbol)
        pos_line = (f"  Broker pos  {pos['qty']:g} @ {pos['avg_entry_price']:.2f} "
                    f"(unreal {pos['unrealized_pl']:+,.2f})" if pos
                    else "  Broker pos  flat")
    except Exception as exc:
        pos, pos_line = None, f"  Broker pos  UNKNOWN — lookup failed: {exc}"
    trade = storage.get_open_trade(path=cfg.db_path)

    print(f"\n  Symbol      {cfg.symbol}  ({cfg.bar_minutes}m bars, "
          f"ema{cfg.ema_fast}/{cfg.ema_slow}, rsi<{cfg.rsi_max_entry:g})")
    print(f"  Endpoint    {cfg.base_url}  (paper={cfg.is_paper})")
    print(f"  Market      {'OPEN' if clock['is_open'] else 'closed'}"
          f"   next close {clock['next_close']}")
    print(f"  Equity      ${account['equity']:,.2f}   "
          f"today {account['day_pnl']:+,.2f} ({account['day_pnl_pct']:+.2f}%)")
    print(f"  Buying pwr  ${account['buying_power']:,.2f}")
    print(pos_line)
    print(f"  Bot trade   #{trade['id']} {trade['side']} {trade['qty']:g} @ "
          f"{trade['entry_price']:.2f}" if trade else "  Bot trade   none")
    print(f"  Trades today {bot._trades_today()}/{cfg.max_trades_per_day}"
          f"   cooldown {bot._cooldown_remaining()}s")
    untracked = storage.get_state("untracked_position", path=cfg.db_path)
    if untracked:
        print(f"\n  ⚠  {untracked}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Alpaca paper-trading bot.")
    parser.add_argument("--once", action="store_true", help="run one cycle and exit")
    parser.add_argument("--dry-run", action="store_true", help="never send orders")
    parser.add_argument("--status", action="store_true", help="print state and exit")
    parser.add_argument("--adopt", action="store_true",
                        help="manage the existing broker position as a bot trade")
    parser.add_argument("--close", action="store_true",
                        help="close the tracked position now and exit")
    parser.add_argument("--symbol", help="override the traded symbol")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    overrides = {}
    if args.dry_run:
        overrides["dry_run"] = True
    if args.symbol:
        overrides["symbol"] = args.symbol.upper()
    cfg = Settings(**{**default_settings.__dict__, **overrides}) if overrides else default_settings

    setup_logging(cfg, args.verbose)

    problems = cfg.validate()
    if problems:
        for p in problems:
            log.error(p)
        return 2

    try:
        bot = TradingBot(cfg)
    except Exception as exc:
        log.error("Could not start: %s", exc)
        return 2

    if args.status:
        _print_status(bot)
        return 0

    if args.adopt:
        bot.adopt_position()
        _print_status(bot)
        return 0

    if args.close:
        trade = storage.get_open_trade(path=cfg.db_path)
        if not trade:
            log.info("No tracked trade to close.")
            return 0
        price = bot.broker.latest_price(cfg.symbol) or trade["entry_price"]
        bot._exit(trade, "manual close", price)
        return 0

    if args.once:
        cycle = bot.run_once()
        storage.set_state("heartbeat", storage.utcnow(), path=cfg.db_path)
        log.info("cycle: %s — %s", cycle.get("action"), cycle.get("reason"))
        return 0

    bot.run_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
