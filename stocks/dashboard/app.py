"""Web dashboard for the trading bot.

Serves one page that polls a JSON API. It reads the bot's database and the
Alpaca account; it never places orders, so it is safe to leave running.

    python -m dashboard.app          # http://127.0.0.1:5000
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from flask import Flask, jsonify, render_template

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot import metrics, storage  # noqa: E402
from bot.settings import settings  # noqa: E402

log = logging.getLogger(__name__)

app = Flask(__name__)
app.config["JSON_SORT_KEYS"] = False

# The broker client is built lazily and reused; None means "not reachable".
_broker = None
_broker_error: str | None = None


def get_broker():
    """Returns a cached Broker, or None if credentials/network are unavailable."""
    global _broker, _broker_error
    if _broker is None and _broker_error is None:
        try:
            from bot.broker import Broker

            _broker = Broker(settings)
        except Exception as exc:  # bad creds, offline, live endpoint, ...
            _broker_error = str(exc)
            log.warning("Broker unavailable: %s", exc)
    return _broker


@app.route("/")
def index():
    # The first payload is inlined so the page paints with real data
    # instead of flashing empty tiles while the first fetch resolves.
    return render_template("index.html", symbol=settings.symbol,
                           bootstrap=_inline_json(build_payload()))


def _inline_json(payload: dict) -> str:
    """JSON safe to drop inside a <script> tag.

    Bot messages end up in this payload, so escape the characters that
    could otherwise close the tag early.
    """
    return (json.dumps(payload)
            .replace("<", "\\u003c")
            .replace(">", "\\u003e")
            .replace("&", "\\u0026"))


@app.route("/api/metrics")
def api_metrics():
    return jsonify(build_payload())


def build_payload() -> dict:
    """Everything the page renders, in one payload."""
    storage.init_db(settings.db_path)

    payload: dict = {
        "symbol": settings.symbol,
        "paper": settings.is_paper,
        "dry_run": settings.dry_run,
        "settings": settings.public_dict(),
        "health": metrics.bot_health(settings),
        "stats": metrics.trade_stats(settings),
        "trades": storage.recent_trades(25, path=settings.db_path),
        "equity_curve": storage.equity_curve(300, path=settings.db_path),
        "realized_curve": metrics.realized_pnl_curve(settings),
        "events": storage.recent_events(15, path=settings.db_path),
        "open_trade": storage.get_open_trade(path=settings.db_path),
        "account": None,
        "position": None,
        "market": None,
        "price": None,
        "broker_error": _broker_error,
    }

    broker = get_broker()
    if broker is not None:
        try:
            payload["account"] = broker.account()
            payload["market"] = broker.clock()
            payload["position"] = broker.position(settings.symbol)
            payload["price"] = broker.latest_price(settings.symbol)
        except Exception as exc:
            payload["broker_error"] = str(exc)
            log.warning("Broker call failed: %s", exc)

    # Live unrealized P&L for the trade the bot is managing. It is computed
    # from the tracked trade's own entry and quantity, not from the broker
    # position, so the number always matches what the bot would realize on
    # exit even if the broker position also holds untracked shares.
    open_trade, position = payload["open_trade"], payload["position"]
    if open_trade:
        current = (position or {}).get("current_price") or payload["price"]
        entry, qty = open_trade["entry_price"], open_trade["qty"]
        direction = 1 if open_trade["side"] == "long" else -1
        high_water = open_trade.get("high_water") or entry
        merged = {
            **open_trade,
            "current_price": current,
            "trail_stop": round(high_water * (1 - settings.trailing_stop_pct), 2),
            "hard_stop": round(entry * (1 - settings.stop_loss_pct), 2),
            "broker_qty": (position or {}).get("qty"),
        }
        if current:
            merged["unrealized_pl"] = round((current - entry) * qty * direction, 2)
            merged["unrealized_plpc"] = round(
                (current - entry) / entry * 100 * direction, 2) if entry else 0.0
        payload["open_trade"] = merged

    return payload


@app.route("/api/health")
def api_health():
    return jsonify(metrics.bot_health(settings))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Trading bot dashboard.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5000)
    parser.add_argument("--debug", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)-7s %(message)s")
    storage.init_db(settings.db_path)
    print(f"\n  Dashboard for {settings.symbol} -> http://{args.host}:{args.port}\n")
    app.run(host=args.host, port=args.port, debug=args.debug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
