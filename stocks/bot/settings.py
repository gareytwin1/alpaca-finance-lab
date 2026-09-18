"""Configuration for the trading bot.

Every value can be overridden with an environment variable, so the bot,
the dashboard and any notebook all read the same settings.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, asdict
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Settings:
    # --- account -------------------------------------------------------
    api_key: str = _env_str("ALPACA_API_KEY", "")
    secret_key: str = _env_str("ALPACA_SECRET_KEY", "")
    base_url: str = _env_str("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
    # 'iex' is the free feed; 'sip' needs a paid market-data subscription.
    data_feed: str = _env_str("ALPACA_DATA_FEED", "iex")

    # --- what we trade -------------------------------------------------
    symbol: str = _env_str("BOT_SYMBOL", "SPY")
    bar_minutes: int = _env_int("BOT_BAR_MINUTES", 1)
    lookback_bars: int = _env_int("BOT_LOOKBACK_BARS", 200)
    poll_seconds: int = _env_int("BOT_POLL_SECONDS", 30)

    # --- strategy ------------------------------------------------------
    ema_fast: int = _env_int("BOT_EMA_FAST", 9)
    ema_slow: int = _env_int("BOT_EMA_SLOW", 21)
    rsi_period: int = _env_int("BOT_RSI_PERIOD", 14)
    rsi_max_entry: float = _env_float("BOT_RSI_MAX_ENTRY", 70.0)
    volume_window: int = _env_int("BOT_VOLUME_WINDOW", 20)
    require_volume: bool = _env_bool("BOT_REQUIRE_VOLUME", True)

    # --- risk ----------------------------------------------------------
    position_size_pct: float = _env_float("BOT_POSITION_SIZE_PCT", 0.10)
    trailing_stop_pct: float = _env_float("BOT_TRAILING_STOP_PCT", 0.015)
    stop_loss_pct: float = _env_float("BOT_STOP_LOSS_PCT", 0.02)
    max_trades_per_day: int = _env_int("BOT_MAX_TRADES_PER_DAY", 10)
    daily_loss_limit: float = _env_float("BOT_DAILY_LOSS_LIMIT", 1000.0)
    cooldown_seconds: int = _env_int("BOT_COOLDOWN_SECONDS", 300)
    # Flatten this many minutes before the closing bell (0 disables).
    flat_before_close_minutes: int = _env_int("BOT_FLAT_BEFORE_CLOSE_MINUTES", 10)

    # --- files ---------------------------------------------------------
    db_path: str = _env_str("BOT_DB_PATH", str(BASE_DIR / "trading_bot.db"))
    log_path: str = _env_str("BOT_LOG_PATH", str(BASE_DIR / "app.log"))

    # --- safety --------------------------------------------------------
    # Dry run computes signals and records them but never sends an order.
    dry_run: bool = _env_bool("BOT_DRY_RUN", False)

    @property
    def is_paper(self) -> bool:
        return "paper" in self.base_url

    def validate(self) -> list[str]:
        """Returns a list of problems; empty means the config is usable."""
        problems: list[str] = []
        if not self.api_key or not self.secret_key:
            problems.append("ALPACA_API_KEY and ALPACA_SECRET_KEY must be set.")
        if not self.is_paper:
            problems.append(
                f"ALPACA_BASE_URL={self.base_url!r} is not a paper endpoint. "
                "This bot refuses to run against a live account."
            )
        if self.ema_fast >= self.ema_slow:
            problems.append("BOT_EMA_FAST must be smaller than BOT_EMA_SLOW.")
        if not 0 < self.position_size_pct <= 1:
            problems.append("BOT_POSITION_SIZE_PCT must be between 0 and 1.")
        if self.lookback_bars < self.ema_slow + self.rsi_period:
            problems.append("BOT_LOOKBACK_BARS is too small for the indicators.")
        return problems

    def public_dict(self) -> dict:
        """Settings without the credentials, safe to show in the dashboard."""
        data = asdict(self)
        for secret in ("api_key", "secret_key"):
            data.pop(secret, None)
        return data


settings = Settings()
