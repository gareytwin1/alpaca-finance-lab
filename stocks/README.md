# Trading bot + dashboard

A small Alpaca **paper-trading** bot and a local web dashboard that shows what it
is doing. The bot trades one symbol on an EMA crossover with an RSI filter; the
dashboard reads the bot's database and the Alpaca account and never places orders.

```
stocks/
├── bot/
│   ├── settings.py     configuration, all overridable by environment variable
│   ├── strategy.py     indicators and signals — pure functions, no I/O
│   ├── broker.py       the only module that talks to alpaca-py
│   ├── storage.py      SQLite ledger: trades, equity, state, events
│   ├── metrics.py      win rate, expectancy, drawdown, bot health
│   └── runner.py       the loop: guards → signal → order → bookkeeping
├── dashboard/
│   ├── app.py          Flask: one page plus /api/metrics
│   └── templates/
└── tests/              offline tests (no network, no broker)
```

## Setup

```bash
pip install -r requirements.txt

export ALPACA_API_KEY=...
export ALPACA_SECRET_KEY=...
export ALPACA_BASE_URL=https://paper-api.alpaca.markets
```

The bot **refuses to start** against a non-paper endpoint. That check lives in
`Settings.validate()` and runs before any client is constructed.

## Running

```bash
python -m bot.runner --status     # account, position, guards — changes nothing
python -m bot.runner --dry-run    # evaluate signals and log them, send no orders
python -m bot.runner --once       # a single cycle
python -m bot.runner              # the loop (Ctrl-C stops after the cycle finishes)

python -m dashboard.app           # http://127.0.0.1:5000
```

Run the bot and the dashboard in two terminals. They share `trading_bot.db`,
which is in WAL mode, so the dashboard reads while the bot writes.

## The strategy

Signals are only ever evaluated on **closed** bars — `Broker.bars()` drops the
still-forming bar at the right edge, so a signal cannot flicker mid-bar.

**Enter long** when all three hold on the latest closed bar:

| Condition | Default |
|---|---|
| EMA(fast) crosses above EMA(slow) | 9 / 21 |
| RSI below the ceiling | RSI(14) < 70 |
| Volume above its rolling average | 20-bar average |

**Exit** on whichever comes first:

| Exit | Default |
|---|---|
| Hard stop off the entry price | −2.0% |
| Trailing stop off the high-water mark | −1.5% |
| EMA(fast) crosses back below EMA(slow) | — |
| Flatten before the closing bell | 10 minutes |

The crossover is a *transition*, not a state: entry requires the fast EMA to have
been at or below the slow EMA on the previous bar, so the bot takes one position
per crossover rather than re-buying every bar the trend persists.

Long only. Sizing is `position_size_pct` of buying power, floored to whole shares.

## Risk guards

Checked before every entry; any one of them blocks the trade:

- market closed, or inside the flatten-before-close window
- cooldown still running since the last exit (default 5 minutes)
- daily trade cap reached (default 10)
- daily loss limit hit (default $1,000, measured as equity − last equity)
- an order is still working
- **an untracked position exists** — see below

### Untracked positions

The bot manages only positions it opened and recorded. If the broker reports a
position in the symbol that has no matching row in the ledger, the bot refuses to
trade that symbol and says so on the dashboard, rather than selling shares it did
not buy. Resolve it either way:

```bash
python -m bot.runner --adopt   # manage the existing position as a bot trade
# or close it yourself in Alpaca
```

The reverse case is handled too: if a tracked trade disappears from the broker
(closed by hand, or by a broker-side stop), the next cycle books it closed at the
last price and starts the cooldown.

## Configuration

Every field in `bot/settings.py` reads an environment variable. The ones worth
knowing:

| Variable | Default | Meaning |
|---|---|---|
| `BOT_SYMBOL` | `SPY` | symbol to trade |
| `BOT_BAR_MINUTES` | `1` | bar size |
| `BOT_POLL_SECONDS` | `30` | seconds between cycles |
| `BOT_EMA_FAST` / `BOT_EMA_SLOW` | `9` / `21` | crossover periods |
| `BOT_RSI_MAX_ENTRY` | `70` | entry blocked at or above this RSI |
| `BOT_POSITION_SIZE_PCT` | `0.10` | fraction of buying power per trade |
| `BOT_TRAILING_STOP_PCT` | `0.015` | trailing stop |
| `BOT_STOP_LOSS_PCT` | `0.02` | hard stop |
| `BOT_MAX_TRADES_PER_DAY` | `10` | daily trade cap |
| `BOT_DAILY_LOSS_LIMIT` | `1000` | daily loss limit in dollars |
| `BOT_COOLDOWN_SECONDS` | `300` | wait after an exit |
| `BOT_DRY_RUN` | `false` | evaluate signals, send no orders |
| `ALPACA_DATA_FEED` | `iex` | `iex` is free; `sip` needs a subscription |

## Dashboard

`http://127.0.0.1:5000` — polls `/api/metrics` every 5 seconds and shows equity,
today's P&L, realized P&L, win rate, expectancy and profit factor, the open
position with its live stop levels, an equity curve, a cumulative realized-P&L
curve, the trade log and the bot's event log. It also flags a stale loop, an
unreachable broker and untracked positions.

Credentials are stripped from the payload (`Settings.public_dict()`). The page
binds to localhost only; it has no authentication, so do not expose it.

## Tests

```bash
python -m unittest discover -s tests -v
```

23 tests covering the indicators, every entry and exit path, position sizing and
the ledger's P&L arithmetic. They construct synthetic bars and a temporary
database, so they need no credentials and no network.

## Relationship to the notebooks

`trading_bot.ipynb` is the exploration that this package grew out of, and it
still holds the charting and data-inspection cells. It is not the runnable bot:
it targets `alpaca_trade_api`, which Alpaca has retired, and its trading logic
was never finished. Use `bot/` to trade and the notebook to explore.
