"""Performance metrics derived from the trade log.

Read-only: safe to call from the dashboard while the bot is trading.
"""

from __future__ import annotations

from datetime import datetime, timezone

from . import storage
from .settings import Settings, settings as default_settings


def _safe_div(a: float, b: float) -> float:
    return a / b if b else 0.0


def trade_stats(cfg: Settings = default_settings) -> dict:
    """Win rate, expectancy, profit factor and drawdown over closed trades."""
    trades = storage.closed_trades(path=cfg.db_path)
    pnls = [float(t["pnl"] or 0.0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    gross_profit = sum(wins)
    gross_loss = abs(sum(losses))

    # Max drawdown of the cumulative realized-P&L curve.
    peak = 0.0
    equity = 0.0
    max_dd = 0.0
    for p in pnls:
        equity += p
        peak = max(peak, equity)
        max_dd = max(max_dd, peak - equity)

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    today_pnl = sum(
        float(t["pnl"] or 0.0) for t in trades
        if (t["exit_time"] or "")[:10] == today
    )

    return {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(_safe_div(len(wins), len(trades)) * 100, 1),
        "total_pnl": round(sum(pnls), 2),
        "realized_today": round(today_pnl, 2),
        "avg_win": round(_safe_div(gross_profit, len(wins)), 2),
        "avg_loss": round(-_safe_div(gross_loss, len(losses)), 2),
        "best_trade": round(max(pnls), 2) if pnls else 0.0,
        "worst_trade": round(min(pnls), 2) if pnls else 0.0,
        "profit_factor": round(_safe_div(gross_profit, gross_loss), 2) if gross_loss else None,
        "expectancy": round(_safe_div(sum(pnls), len(pnls)), 2),
        "max_drawdown": round(max_dd, 2),
        "trades_today": storage.count_trades_on(today, path=cfg.db_path),
    }


def realized_pnl_curve(cfg: Settings = default_settings) -> list[dict]:
    """Cumulative realized P&L, one point per closed trade."""
    cumulative = 0.0
    points = []
    for t in storage.closed_trades(path=cfg.db_path):
        cumulative += float(t["pnl"] or 0.0)
        points.append({
            "trade_id": t["id"],
            "exit_time": t["exit_time"],
            "pnl": round(float(t["pnl"] or 0.0), 2),
            "cumulative": round(cumulative, 2),
        })
    return points


def bot_health(cfg: Settings = default_settings) -> dict:
    """Whether the loop is alive, based on the heartbeat it writes each cycle."""
    state = storage.all_state(path=cfg.db_path)
    heartbeat = state.get("heartbeat")
    age = None
    if heartbeat:
        try:
            beat = datetime.fromisoformat(heartbeat)
            if beat.tzinfo is None:
                beat = beat.replace(tzinfo=timezone.utc)
            age = (datetime.now(timezone.utc) - beat).total_seconds()
        except ValueError:
            age = None

    # Two missed polls is the line between "running" and "stale".
    stale_after = max(cfg.poll_seconds * 2.5, 90)
    if age is None:
        status = "never run"
    elif age <= stale_after:
        status = "running"
    else:
        status = "stale"

    return {
        "status": status,
        "heartbeat": heartbeat,
        "seconds_since_heartbeat": round(age) if age is not None else None,
        "last_exit": state.get("last_exit"),
        "untracked_position": state.get("untracked_position"),
        "last_cycle": state.get("last_cycle"),
    }
