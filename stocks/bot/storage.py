"""SQLite persistence for trades, equity snapshots and bot state.

The bot writes and the dashboard reads, so the database runs in WAL mode:
readers never block the writer and never see a half-finished transaction.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Iterator

from .settings import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
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

CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS idx_trades_entry  ON trades(entry_time);

CREATE TABLE IF NOT EXISTS equity_snapshots (
    ts       TEXT PRIMARY KEY,
    equity   REAL NOT NULL,
    cash     REAL,
    day_pnl  REAL
);

CREATE TABLE IF NOT EXISTS bot_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      TEXT NOT NULL,
    level   TEXT NOT NULL,
    message TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
"""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connect(path: str | None = None, readonly: bool = False) -> Iterator[sqlite3.Connection]:
    """Yields a connection with row access by name and WAL enabled."""
    db_path = path or settings.db_path
    if readonly:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5)
    else:
        conn = sqlite3.connect(db_path, timeout=15)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        if not readonly:
            conn.commit()
    finally:
        conn.close()


def init_db(path: str | None = None) -> None:
    with connect(path) as conn:
        conn.executescript(SCHEMA)


# --------------------------------------------------------------------------
# bot state (small key/value bag: last_exit, heartbeat, last_signal, ...)
# --------------------------------------------------------------------------

def set_state(key: str, value: Any, path: str | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO bot_state(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value)),
        )


def get_state(key: str, default: Any = None, path: str | None = None) -> Any:
    with connect(path, readonly=True) as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key=?", (key,)).fetchone()
    if row is None:
        return default
    try:
        return json.loads(row["value"])
    except (TypeError, ValueError):
        return default


def all_state(path: str | None = None) -> dict[str, Any]:
    with connect(path, readonly=True) as conn:
        rows = conn.execute("SELECT key, value FROM bot_state").fetchall()
    out: dict[str, Any] = {}
    for row in rows:
        try:
            out[row["key"]] = json.loads(row["value"])
        except (TypeError, ValueError):
            out[row["key"]] = row["value"]
    return out


# --------------------------------------------------------------------------
# trades
# --------------------------------------------------------------------------

def open_trade(
    symbol: str,
    side: str,
    qty: float,
    entry_price: float,
    entry_reason: str = "",
    entry_time: str | None = None,
    path: str | None = None,
) -> int:
    with connect(path) as conn:
        cur = conn.execute(
            "INSERT INTO trades(symbol, side, qty, entry_time, entry_price, "
            "entry_reason, high_water, status) VALUES(?,?,?,?,?,?,?, 'open')",
            (symbol, side, qty, entry_time or utcnow(), entry_price,
             entry_reason, entry_price),
        )
        return int(cur.lastrowid)


def get_open_trade(path: str | None = None) -> dict | None:
    with connect(path, readonly=True) as conn:
        row = conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def update_high_water(trade_id: int, high: float, path: str | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "UPDATE trades SET high_water = MAX(COALESCE(high_water, 0), ?) WHERE id = ?",
            (high, trade_id),
        )


def close_trade(
    trade_id: int,
    exit_price: float,
    exit_reason: str = "",
    exit_time: str | None = None,
    path: str | None = None,
) -> dict | None:
    """Closes a trade and computes its realized P&L. Returns the closed row."""
    with connect(path) as conn:
        row = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
        if row is None:
            return None
        direction = 1 if row["side"] == "long" else -1
        pnl = (exit_price - row["entry_price"]) * row["qty"] * direction
        pnl_pct = (
            (exit_price - row["entry_price"]) / row["entry_price"] * 100 * direction
            if row["entry_price"] else 0.0
        )
        conn.execute(
            "UPDATE trades SET exit_time=?, exit_price=?, exit_reason=?, "
            "pnl=?, pnl_pct=?, status='closed' WHERE id=?",
            (exit_time or utcnow(), exit_price, exit_reason, pnl, pnl_pct, trade_id),
        )
        closed = conn.execute("SELECT * FROM trades WHERE id=?", (trade_id,)).fetchone()
    return dict(closed) if closed else None


def recent_trades(limit: int = 50, path: str | None = None) -> list[dict]:
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM trades ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def closed_trades(path: str | None = None) -> list[dict]:
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY id ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def count_trades_on(day_iso: str, path: str | None = None) -> int:
    """Number of positions opened on a given UTC date (YYYY-MM-DD)."""
    with connect(path, readonly=True) as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS n FROM trades WHERE substr(entry_time, 1, 10) = ?",
            (day_iso,),
        ).fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------
# equity + events
# --------------------------------------------------------------------------

def record_equity(
    equity: float, cash: float | None = None, day_pnl: float | None = None,
    ts: str | None = None, path: str | None = None,
) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO equity_snapshots(ts, equity, cash, day_pnl) VALUES(?,?,?,?) "
            "ON CONFLICT(ts) DO UPDATE SET equity=excluded.equity, "
            "cash=excluded.cash, day_pnl=excluded.day_pnl",
            (ts or utcnow(), equity, cash, day_pnl),
        )


def equity_curve(limit: int = 500, path: str | None = None) -> list[dict]:
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM (SELECT * FROM equity_snapshots ORDER BY ts DESC LIMIT ?) "
            "ORDER BY ts ASC",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def log_event(level: str, message: str, path: str | None = None) -> None:
    with connect(path) as conn:
        conn.execute(
            "INSERT INTO events(ts, level, message) VALUES(?,?,?)",
            (utcnow(), level.upper(), message),
        )


def recent_events(limit: int = 50, path: str | None = None) -> list[dict]:
    with connect(path, readonly=True) as conn:
        rows = conn.execute(
            "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]
