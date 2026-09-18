"""EMA-crossover strategy with an RSI filter.

Pure functions over a bars DataFrame: no network, no broker, no database,
so the whole strategy can be unit-tested and backtested offline.

Entry (long):  EMA(fast) crosses above EMA(slow), RSI below the ceiling,
               and volume above its rolling average.
Exit:          EMA(fast) crosses back below EMA(slow), a trailing stop off
               the high water mark, or a hard stop off the entry price.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from .settings import Settings, settings as default_settings

Action = Literal["buy", "sell", "hold"]


@dataclass
class Signal:
    action: Action = "hold"
    reason: str = ""
    price: float = 0.0
    indicators: dict = field(default_factory=dict)

    def __bool__(self) -> bool:
        return self.action != "hold"


# --------------------------------------------------------------------------
# indicators
# --------------------------------------------------------------------------

def ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI. Returns values in [0, 100]; NaN until `period` bars."""
    delta = series.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    # Wilder smoothing is an EMA with alpha = 1/period.
    avg_gain = gain.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0.0, np.nan)
    out = 100 - (100 / (1 + rs))
    # A flat-or-rising window with zero losses is maximally overbought.
    return out.fillna(100.0).where(avg_gain.notna(), other=np.nan)


def add_indicators(df: pd.DataFrame, cfg: Settings = default_settings) -> pd.DataFrame:
    """Returns a copy of `df` with ema_fast, ema_slow, rsi and avg_volume."""
    if df.empty:
        raise ValueError("Cannot compute indicators on an empty DataFrame.")
    missing = {"close", "volume"} - set(df.columns)
    if missing:
        raise KeyError(f"Bars are missing required columns: {sorted(missing)}")

    out = df.copy()
    out["ema_fast"] = ema(out["close"], cfg.ema_fast)
    out["ema_slow"] = ema(out["close"], cfg.ema_slow)
    out["rsi"] = rsi(out["close"], cfg.rsi_period)
    out["avg_volume"] = out["volume"].rolling(cfg.volume_window, min_periods=1).mean()
    return out


# --------------------------------------------------------------------------
# signals
# --------------------------------------------------------------------------

def _snapshot(prev: pd.Series, last: pd.Series) -> dict:
    return {
        "close": float(last["close"]),
        "ema_fast": float(last["ema_fast"]),
        "ema_slow": float(last["ema_slow"]),
        "rsi": float(last["rsi"]) if pd.notna(last["rsi"]) else None,
        "volume": float(last["volume"]),
        "avg_volume": float(last["avg_volume"]),
        "prev_ema_fast": float(prev["ema_fast"]),
        "prev_ema_slow": float(prev["ema_slow"]),
    }


def entry_signal(df: pd.DataFrame, cfg: Settings = default_settings) -> Signal:
    """Looks for a fresh bullish crossover on the most recent closed bar."""
    if len(df) < max(cfg.ema_slow, cfg.rsi_period) + 2:
        return Signal(reason="not enough bars")

    prev, last = df.iloc[-2], df.iloc[-1]
    snap = _snapshot(prev, last)
    price = snap["close"]

    crossed_up = (
        snap["prev_ema_fast"] <= snap["prev_ema_slow"]
        and snap["ema_fast"] > snap["ema_slow"]
    )
    if not crossed_up:
        trend = "above" if snap["ema_fast"] > snap["ema_slow"] else "below"
        return Signal(reason=f"no crossover (fast {trend} slow)",
                      price=price, indicators=snap)

    if snap["rsi"] is None:
        return Signal(reason="rsi not warmed up", price=price, indicators=snap)

    if snap["rsi"] >= cfg.rsi_max_entry:
        return Signal(
            reason=f"crossover but rsi {snap['rsi']:.1f} >= {cfg.rsi_max_entry:.0f}",
            price=price, indicators=snap,
        )

    if cfg.require_volume and snap["volume"] <= snap["avg_volume"]:
        return Signal(reason="crossover but volume below average",
                      price=price, indicators=snap)

    return Signal(
        action="buy",
        reason=(f"ema{cfg.ema_fast} crossed above ema{cfg.ema_slow}, "
                f"rsi {snap['rsi']:.1f}"),
        price=price,
        indicators=snap,
    )


def exit_signal(
    df: pd.DataFrame,
    entry_price: float,
    high_water: float,
    cfg: Settings = default_settings,
) -> Signal:
    """Checks the three long exits: crossdown, trailing stop, hard stop."""
    if len(df) < 2:
        return Signal(reason="not enough bars")

    prev, last = df.iloc[-2], df.iloc[-1]
    snap = _snapshot(prev, last)
    price = snap["close"]

    high_water = max(high_water, price)
    trail_stop = high_water * (1 - cfg.trailing_stop_pct)
    hard_stop = entry_price * (1 - cfg.stop_loss_pct)
    snap.update(high_water=high_water, trail_stop=trail_stop, hard_stop=hard_stop)

    if price <= hard_stop:
        return Signal("sell", f"stop loss hit ({price:.2f} <= {hard_stop:.2f})",
                      price, snap)

    if price <= trail_stop:
        return Signal("sell",
                      f"trailing stop hit ({price:.2f} <= {trail_stop:.2f}, "
                      f"high {high_water:.2f})", price, snap)

    crossed_down = (
        snap["prev_ema_fast"] >= snap["prev_ema_slow"]
        and snap["ema_fast"] < snap["ema_slow"]
    )
    if crossed_down:
        return Signal("sell",
                      f"ema{cfg.ema_fast} crossed below ema{cfg.ema_slow}",
                      price, snap)

    return Signal(reason=f"holding (trail stop {trail_stop:.2f})",
                  price=price, indicators=snap)


def position_size(buying_power: float, price: float,
                  cfg: Settings = default_settings) -> int:
    """Whole shares for `position_size_pct` of buying power."""
    if price <= 0 or buying_power <= 0:
        return 0
    return int((buying_power * cfg.position_size_pct) // price)
