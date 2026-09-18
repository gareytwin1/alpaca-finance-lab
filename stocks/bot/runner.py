"""The trading loop: guards, signals, orders and bookkeeping.

Run it with::

    python -m bot.runner            # live paper loop
    python -m bot.runner --once     # a single cycle, then exit
    python -m bot.runner --dry-run  # evaluate signals, never send orders
    python -m bot.runner --status   # print state and exit
"""

from __future__ import annotations

import argparse
import fcntl
import logging
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

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


@contextmanager
def single_instance(cfg: Settings) -> Iterator[None]:
    """Holds an exclusive lock for as long as this process may trade.

    Two runners against one account both read "no close order is working",
    both submit one, and the second write of `pending_exit` hides the first
    order entirely. SQLite serializes each statement but not the read, the
    broker call and the write as a unit, so the guard has to live out here.
    """
    lock_path = f"{cfg.db_path}.lock"
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        raise RuntimeError(
            f"another bot process is already running against {cfg.db_path} "
            f"(lock: {lock_path})"
        )
    try:
        os.ftruncate(fd, 0)
        os.write(fd, f"{os.getpid()}\n".encode())
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


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

    # -- partial exits ----------------------------------------------------

    def _exit_fills(self, trade_id: int) -> list[dict]:
        """Shares already sold by earlier close orders for this trade."""
        record = storage.get_state("exit_fills", path=self.cfg.db_path) or {}
        if record.get("trade_id") != trade_id:
            return []
        return [f for f in record.get("fills") or []
                if f.get("qty") and f.get("price")]

    def _record_partial_fill(self, trade_id: int, order: dict) -> None:
        """Saves a dying order's executions before its id is thrown away.

        A close order can be canceled or expire after filling part of the
        position. Those shares are gone at the broker, so losing them here
        would price the whole trade off whatever the replacement order fills
        at, silently misstating realized P&L.
        """
        qty = order.get("filled_qty") or 0
        price = order.get("filled_avg_price")
        if not qty or not price:
            return
        fills = self._exit_fills(trade_id)
        if any(f.get("order_id") == order["id"] for f in fills):
            return
        fills.append({"order_id": order["id"], "qty": qty, "price": price})
        self._state("exit_fills", {"trade_id": trade_id, "fills": fills})
        self._note("warning",
                   f"Trade #{trade_id}: order {order['id']} filled {qty:g} @ "
                   f"{price:.2f} before it died; carrying that into the exit.")

    def _aggregate_exit(self, trade_id: int, qty: float,
                        price: float) -> tuple[float, float]:
        """Blends earlier partial exits with a final fill into one average."""
        fills = self._exit_fills(trade_id) + [{"qty": qty, "price": price}]
        total = sum(f["qty"] for f in fills)
        if total <= 0:
            return price, 0.0
        return sum(f["qty"] * f["price"] for f in fills) / total, total

    # -- in-flight closes -------------------------------------------------

    def _pending_exit(self, trade_id: int) -> dict:
        """The recorded in-flight close for this trade, or an empty dict."""
        pending = storage.get_state("pending_exit", path=self.cfg.db_path) or {}
        if pending.get("trade_id") != trade_id:
            return {}
        if not (pending.get("client_order_id") or pending.get("order_id")):
            return {}
        return pending

    def _resolve_pending(self, pending: dict) -> dict | None:
        """The order a pending record names, by whichever id it carries.

        Records written before exits carried a client id hold only an order
        id. Resolving those the old way keeps a close that is still working
        visible across the upgrade, instead of looking like no close at all
        and inviting a duplicate.
        """
        if pending.get("client_order_id"):
            return self.broker.get_order_by_client_id(pending["client_order_id"])
        return self.broker.get_order(pending["order_id"])

    # -- reconciliation --------------------------------------------------

    def reconcile(self, broker_pos: dict | None, db_trade: dict | None,
                  price: float | None) -> tuple[dict | None, str | None]:
        """Aligns our books with the broker's.

        Returns the trade we may manage plus a note about anything odd. A
        position the bot did not open is never touched, and one whose size or
        side disagrees with the ledger is reported rather than managed.
        """
        if db_trade and not broker_pos:
            # The position is gone. If our own close order is what filled,
            # book its actual fill price rather than the last trade price.
            trade_id = db_trade["id"]
            pending = self._pending_exit(trade_id)
            if pending:
                ref = pending.get("client_order_id") or pending.get("order_id")
                try:
                    order = self._resolve_pending(pending)
                except Exception as exc:
                    # Cannot tell whose fill this was; leave the trade open
                    # rather than booking a guessed price.
                    self._note("warning",
                               f"Trade #{trade_id}: position is gone but close "
                               f"order {ref} is unreadable ({exc}). Leaving the "
                               f"trade open.")
                    return None, "exit outcome unknown"
                if order and order["status"].lower() in self.broker.FILLED \
                        and order.get("filled_avg_price"):
                    booked = self._book_exit(
                        db_trade, order,
                        pending.get("reason") or "closed at the broker")
                    if booked["action"] != "sell":
                        return None, "exit outcome unknown"
                    return None, "orphaned trade reconciled"

            # Nobody can say what the remaining shares fetched, so the last
            # trade price is a guess; any confirmed partials still count.
            exited = sum(f["qty"] for f in self._exit_fills(trade_id))
            remaining = max(db_trade["qty"] - exited, 0.0)
            exit_price, exit_qty = self._aggregate_exit(
                trade_id, remaining, price or db_trade["entry_price"])
            storage.close_trade(trade_id, exit_price, "closed outside the bot",
                                path=self.cfg.db_path,
                                exit_qty=exit_qty or None)
            self._state("pending_exit", None)
            self._state("exit_fills", None)
            self._state("last_exit", storage.utcnow())
            self._note("warning",
                       f"Trade #{trade_id} was closed outside the bot; "
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

        if broker_pos and db_trade:
            mismatch = self._position_mismatch(broker_pos, db_trade)
            known = storage.get_state("position_mismatch", path=self.cfg.db_path)
            if mismatch:
                # Every cycle re-detects the same mismatch; log it once.
                if mismatch != known:
                    self._note("error", mismatch)
                self._state("position_mismatch", mismatch)
                return None, mismatch
            if known:
                self._note("info", f"Trade #{db_trade['id']} matches the broker "
                                   f"again; resuming management.")
            self._state("position_mismatch", None)

        self._state("untracked_position", None)
        return db_trade, None

    def _position_mismatch(self, broker_pos: dict, db_trade: dict) -> str | None:
        """Why the live position cannot be managed as this trade, if so.

        Closing is all-or-nothing at the broker, so a position that is bigger,
        smaller or the other way round than the ledger says is not something
        long-only exit logic may act on: it would sell shares the bot does not
        own or price a close against the wrong share count.
        """
        trade_id = db_trade["id"]
        if broker_pos["side"] != db_trade["side"]:
            return (f"Broker holds a {broker_pos['side']} position in "
                    f"{broker_pos['symbol']} but trade #{trade_id} is "
                    f"{db_trade['side']}. This bot is long-only and will not "
                    f"manage a side it did not open; resolve it manually.")

        exited = sum(f["qty"] for f in self._exit_fills(trade_id))
        expected = db_trade["qty"] - exited
        difference = broker_pos["qty"] - expected
        if abs(difference) < 1e-6:
            return None

        if difference > 0:
            return (f"Broker holds {broker_pos['qty']:g} "
                    f"{broker_pos['symbol']} but trade #{trade_id} accounts "
                    f"for {expected:g}. Closing would liquidate "
                    f"{difference:g} shares the bot does not own; the bot "
                    f"will not trade this symbol until it is resolved.")

        if self._pending_exit(trade_id):
            # A close is in flight; a shrinking position is what it does. This
            # holds for a submission whose response was lost too — the order
            # may well exist, so its effect on the position is expected.
            return None

        return (f"Broker holds {broker_pos['qty']:g} {broker_pos['symbol']} "
                f"but trade #{trade_id} expects {expected:g} and no close "
                f"order is working. Something sold behind the bot; it will "
                f"not trade this symbol until it is resolved.")

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

        try:
            fill = self.broker.wait_for_fill(order["id"])
        except Exception as exc:
            # The buy is already at the broker. Do not record a trade we
            # cannot price, and do not assume it failed: if it filled, the
            # untracked-position guard catches it on the next cycle.
            self._note("error",
                       f"Buy order {order['id']} submitted but its status is "
                       f"unreadable ({exc}). Not recording a trade; if it "
                       f"filled it will surface as an untracked position.")
            return {"action": "entry-unconfirmed", "order_id": order["id"],
                    "reason": f"status lookup failed: {exc}"}

        if not fill or fill["status"].lower() != "filled":
            status = fill["status"] if fill else "missing"
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

    def _book_exit(self, db_trade: dict, order: dict, reason: str) -> dict:
        """Records a confirmed fill. The only path that closes a ledger trade."""
        cfg = self.cfg
        trade_id = db_trade["id"]
        exit_price = order.get("filled_avg_price")
        if not exit_price:
            # "filled" with no average price should not happen; refuse to
            # invent one rather than book a fabricated P&L.
            self._note("error",
                       f"Exit for #{trade_id}: order {order['id']} reports "
                       f"filled but carries no fill price. Trade stays open.")
            return {"action": "exit-unconfirmed", "trade_id": trade_id,
                    "order_id": order["id"],
                    "reason": "filled order had no average price"}

        prior = self._exit_fills(trade_id)
        final_qty = order.get("filled_qty") or 0
        if prior and not final_qty:
            self._note("error",
                       f"Exit for #{trade_id}: order {order['id']} reports "
                       f"filled but carries no quantity, and {len(prior)} "
                       f"earlier fill(s) must be weighted against it. "
                       f"Trade stays open.")
            return {"action": "exit-unconfirmed", "trade_id": trade_id,
                    "order_id": order["id"],
                    "reason": "filled order had no quantity to weight"}

        exit_qty = None
        if prior:
            exit_price, exit_qty = self._aggregate_exit(trade_id, final_qty,
                                                        exit_price)

        closed = storage.close_trade(trade_id, exit_price, reason,
                                     path=cfg.db_path, exit_qty=exit_qty)
        self._state("pending_exit", None)
        self._state("exit_fills", None)
        self._state("last_exit", storage.utcnow())
        pnl = (closed or {}).get("pnl", 0.0) or 0.0
        self._note("info",
                   f"EXIT #{db_trade['id']} @ {exit_price:.2f} — {reason} | "
                   f"P&L {pnl:+.2f} | order {order['id']}")
        return {"action": "sell", "reason": reason, "fill_price": exit_price,
                "pnl": pnl, "trade_id": db_trade["id"], "order_id": order["id"]}

    def _unconfirmed(self, db_trade: dict, order_id: str, status: str,
                     detail: str) -> dict:
        """Leaves the trade open and says exactly what is unresolved."""
        self._note("warning",
                   f"Exit for #{db_trade['id']} NOT confirmed: order {order_id} "
                   f"last status {status!r} ({detail}). Ledger trade stays open; "
                   f"retrying next cycle.")
        return {"action": "exit-unconfirmed", "trade_id": db_trade["id"],
                "order_id": order_id, "status": status, "reason": detail}

    def _exit(self, db_trade: dict, reason: str, price: float) -> dict:
        """Closes a position, booking it only once the broker confirms a fill.

        An exit is a two-phase thing: an order is submitted, and separately it
        settles. The ledger follows the second phase, never the first, so a
        timeout or an outage leaves the trade open and retryable instead of
        recording an exit that did not happen.
        """
        cfg = self.cfg
        trade_id = db_trade["id"]

        if cfg.dry_run:
            self._note("info", f"[DRY RUN] would SELL trade #{trade_id} @ ~{price:.2f} ({reason})")
            return {"action": "dry-run-sell", "reason": reason}

        # --- phase 1: what happened to the close we may already have sent? --
        pending = self._pending_exit(trade_id)
        attempt = 1
        reuse_id = None
        if pending:
            attempt = pending.get("attempt") or 1
            reason = pending.get("reason") or reason
            ref = pending.get("client_order_id") or pending["order_id"]
            try:
                order = self._resolve_pending(pending)
            except Exception as exc:
                return self._unconfirmed(db_trade, ref, "unreadable",
                                         f"status lookup failed: {exc}")

            if order is None:
                # Alpaca has no such order, so the earlier submission never
                # landed however it failed. A client id can be reused here
                # precisely because nothing exists to collide with.
                reuse_id = pending.get("client_order_id")
                self._note("warning",
                           f"Exit for #{trade_id}: no order exists for {ref}; "
                           f"the earlier submission never reached Alpaca. "
                           f"Submitting again.")
            elif order["status"].lower() in self.broker.FILLED:
                return self._book_exit(db_trade, order, reason)
            elif self.broker.is_working(order["status"]):
                # Still live at the broker. A second close here would
                # oversell, so wait for this one instead.
                self._note("info",
                           f"Exit for #{trade_id}: order {ref} still "
                           f"{order['status']!r}; not sending another.")
                return {"action": "exit-pending", "trade_id": trade_id,
                        "order_id": order.get("id"),
                        "client_order_id": pending.get("client_order_id"),
                        "status": order["status"],
                        "reason": f"close order {order['status']}"}
            else:
                self._record_partial_fill(trade_id, order)
                done = order.get("filled_qty") or 0
                self._note("warning",
                           f"Exit for #{trade_id}: order {ref} ended "
                           f"{order['status']!r} after filling {done:g}/"
                           f"{db_trade['qty']:g}; resubmitting for the rest.")
                # A dead order keeps its id forever, so the replacement needs
                # a new one.
                attempt += 1
                self._state("pending_exit", None)

        # --- phase 2: submit a close ----------------------------------------
        client_order_id = reuse_id or f"bot-exit-{trade_id}-{attempt}"
        # Persist the id BEFORE the request. If the response is lost, this is
        # the only thread back to an order Alpaca may already have accepted.
        self._state("pending_exit", {"trade_id": trade_id, "attempt": attempt,
                                     "client_order_id": client_order_id,
                                     "order_id": None, "reason": reason,
                                     "submitted_at": storage.utcnow()})

        self.broker.cancel_open_orders(cfg.symbol)

        try:
            position = self.broker.position(cfg.symbol)
        except Exception as exc:
            return self._unconfirmed(db_trade, client_order_id, "unreadable",
                                     f"position lookup failed: {exc}")
        if not position:
            return self._unconfirmed(db_trade, client_order_id, "missing",
                                     "broker reports no position to close")

        try:
            result = self.broker.submit_market_order(
                cfg.symbol, "sell", position["qty"],
                client_order_id=client_order_id)
        except Exception as exc:
            # Ambiguous: Alpaca may have accepted this before the response was
            # lost. The id is already persisted, so the next cycle asks Alpaca
            # what happened instead of guessing.
            return self._unconfirmed(db_trade, client_order_id, "unsubmitted",
                                     f"close submission failed ({exc}); "
                                     f"resolving by client id next cycle")

        order_id = result["id"]
        self._state("pending_exit", {"trade_id": trade_id, "attempt": attempt,
                                     "client_order_id": client_order_id,
                                     "order_id": order_id, "reason": reason,
                                     "submitted_at": storage.utcnow()})

        # --- phase 3: confirm ------------------------------------------------
        try:
            order = self.broker.wait_for_fill(order_id)
        except Exception as exc:
            return self._unconfirmed(db_trade, order_id, "unreadable",
                                     f"status lookup failed: {exc}")

        if order is None:
            return self._unconfirmed(db_trade, order_id, "missing",
                                     "order vanished after submission")
        if order["status"].lower() in self.broker.FILLED:
            return self._book_exit(db_trade, order, reason)

        filled = order.get("filled_qty") or 0
        detail = ("partially filled "
                  f"{filled:g}/{db_trade['qty']:g}" if filled
                  else "did not fill before the confirmation timeout")
        return self._unconfirmed(db_trade, order_id, order["status"], detail)

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
    for key in ("untracked_position", "position_mismatch"):
        warning = storage.get_state(key, path=cfg.db_path)
        if warning:
            print(f"\n  ⚠  {warning}")
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

    try:
        with single_instance(cfg):
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
    except RuntimeError as exc:
        log.error("Refusing to start: %s", exc)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
