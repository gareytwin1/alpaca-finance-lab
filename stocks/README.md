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
`Settings.validate()` and runs before any client is constructed. There is no
separate flag or config value that allows live trading — the only way to trade
a live account would be to point `ALPACA_BASE_URL` at one, which `validate()`
rejects outright.

## Running

```bash
python -m bot.runner --status     # account, position, guards — changes nothing
python -m bot.runner --dry-run    # evaluate signals and log them, send no orders
python -m bot.runner --once       # a single cycle
python -m bot.runner              # the loop (Ctrl-C stops after the cycle finishes)
python -m bot.runner --adopt      # manage an existing broker position as a trade
python -m bot.runner --close      # close the tracked trade now, then exit

python -m dashboard.app           # http://127.0.0.1:5000
```

Run the bot and the dashboard in two terminals. They share `trading_bot.db`,
which is in WAL mode, so the dashboard reads while the bot writes.

**Only one bot process may run against a given database at a time.** Every
order-producing path (`--once`, `--adopt`, `--close`, and the default loop)
takes an exclusive `flock` on `<db_path>.lock` before doing anything; a second
process against the same `BOT_DB_PATH` refuses to start and exits with an
error rather than racing the first one. `--status` does not take the lock,
since it never places an order. See **Known limitations** for what this
lock does *not* protect against.

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

Long only. Position size is `position_size_pct` of the account's *buying power*
at the moment of entry, floored to whole shares (`bot/strategy.py:position_size`)
— it is not based on the trade's risk (e.g. distance to the stop-loss), so the
dollar risk per trade varies with where the stop actually gets hit.

## Order lifecycle

An order being **submitted** and an order **settling** are treated as separate
events; the ledger only ever follows the second one.

**Entry** (`TradingBot._enter`): submits a market buy, then polls
(`Broker.wait_for_fill`) until it reaches a terminal state or the poll times
out. A trade row is written to the ledger **only if the fill is confirmed and
carries a fill price** — a rejected, unfilled, or unconfirmable buy books
nothing. If the order was accepted by Alpaca but the bot never learns the
outcome (see *ambiguous submission* below), no trade is recorded; if the order
did fill, the next cycle's reconciliation discovers it as an **untracked
position** rather than silently losing track of it.

**Exit** (`TradingBot._exit`) runs in three phases, re-entered every cycle
until it resolves:

1. **Is a close order already working?** The bot keeps the in-flight order's
   id in the `pending_exit` bot-state key. If that order is still open, the
   bot waits — it does not submit a second close. If it's already `filled`,
   the trade is booked immediately (no order duplicated by simply calling
   `_exit()` again). If it died without filling (canceled, expired,
   `done_for_day`, rejected, suspended) it may still have partially executed
   — see **Partial-fill accounting** below — and the bot clears `pending_exit`
   and moves to phase 2.
2. **Submit a close.** Cancels any other open orders for the symbol, then
   calls `close_position()`. The resulting order id is written to
   `pending_exit` **before** the bot waits on it, so a crash mid-wait doesn't
   lose track of the order.
3. **Confirm.** Polls for a fill. A trade is only closed
   (`storage.close_trade`) once a `filled` status with a real fill price
   comes back. Anything else — timeout, partial fill, an unreadable broker,
   the order vanishing — leaves the ledger trade **open** and the cycle
   returns `exit-unconfirmed`; the next cycle re-enters phase 1 and keeps
   trying against the same order id.

A timeout, a dropped connection, or an outage during any of this **never**
books an exit on its own — the ledger only changes on a confirmed fill.

### Partial-fill accounting (`exit_fills` / `exit_qty`)

A close order can die (get canceled, expire, or run out its `time_in_force`)
after filling only part of the position — the remaining shares then get
closed by a *second* order at a different price. If the ledger priced the
whole exit off only the second order's fill, it would silently misstate the
realized P&L.

When a close order dies with a nonzero `filled_qty`, the bot records
`{order_id, qty, price}` under the `exit_fills` bot-state key
(`TradingBot._record_partial_fill`) before discarding the order id. When the
trade finally closes, `TradingBot._aggregate_exit` combines every recorded
partial with the final fill into one quantity-weighted average price, and
that average — along with the true total quantity — is what gets written to
the ledger as `exit_price` / `exit_qty`. `exit_qty` is a new `trades` column;
older closed rows have it as `NULL` (nothing reads it back out for P&L math —
`pnl` was already computed and stored at close time — so this is safe; see
**Database migration**).

Recording is keyed by order id, so re-observing the same dead order twice
(e.g. after a crash between recording the fill and clearing `pending_exit`)
does not double-count it. A single order's own `filled_qty` is read only once
it is already terminal, at which point Alpaca's cumulative count for that
order will not change again.

### Quantity and side reconciliation

Every cycle, before doing anything else, `TradingBot.reconcile()` compares
the ledger's open trade against the broker's actual position:

| Ledger | Broker | Result |
|---|---|---|
| open trade | no position | Position is gone. If the bot's own tracked close order is what filled, book the confirmed price (aggregating any partials). Otherwise, book at the last known price plus any recorded partials — this specific case is a **guess**, not a confirmed fill. |
| no trade | a position exists | **Untracked position** — see below. The bot will not touch it. |
| open trade | position exists, but the **side** differs (e.g. ledger long, broker short) | **Blocked.** This bot is long-only and will not manage a side it did not open. |
| open trade | position exists, broker qty is **larger** than the ledger + recorded exits expect | **Blocked.** Closing would liquidate shares the bot doesn't own (e.g. manually-added shares in the same symbol). |
| open trade | position exists, broker qty is **smaller** than expected | Allowed to proceed **only if** a close order is currently working (`pending_exit` set) for this trade — a shrinking position is exactly what that order does. Otherwise **blocked**: something sold behind the bot. |
| open trade | position matches (qty and side, within floating-point tolerance) | Managed normally. |

A blocked mismatch is sticky (`position_mismatch` bot-state key) and is logged
once per new mismatch, not every cycle. **A known gap:** when a close order is
in flight, the "smaller than expected" case is currently accepted without
checking that the size of the shortfall is actually consistent with what
`exit_fills` has recorded so far — see **Known limitations**.

### Untracked positions

The bot manages only positions it opened and recorded. If the broker reports a
position in the symbol with no matching ledger row, the bot refuses to trade
that symbol and says so (`untracked_position` bot-state key, shown on both
`--status` and the dashboard). Resolve it either way:

```bash
python -m bot.runner --adopt   # manage the existing position as a bot trade
# or close it yourself in Alpaca
```

## Single-instance locking

`bot.runner.single_instance()` takes a non-blocking `flock` on
`<db_path>.lock` for the duration of any order-producing command. A second
process against the *same database file* is refused at startup (exit code 2)
rather than racing the first one for `pending_exit`/broker state. `--status`
never takes the lock.

This protects one thing specifically: two runners sharing one `BOT_DB_PATH`.
It does **not** know about Alpaca accounts or symbols — see
**Known limitations**.

## Database migration

`storage.init_db()` runs `CREATE TABLE IF NOT EXISTS` for the whole schema and
then a small migration step that adds any column an older database is
missing (currently just `trades.exit_qty`), gated on `PRAGMA table_info`
rather than a version number. This runs on every `TradingBot` construction,
so it's applied automatically the first time you run any bot command against
an existing database — no separate migration command exists or is needed.
It is safe to run against an empty database, an already-migrated one, and
concurrently from two processes racing to start against the same file (SQLite
serializes the `ALTER TABLE`; the loser's schema check then finds the column
already present and skips it).

Back up `trading_bot.db` before a schema change you're unsure about — it's a
single file; `cp trading_bot.db trading_bot.db.bak` is sufficient.

## Risk guards

Checked before every entry; any one of them blocks the trade:

- market closed, or inside the flatten-before-close window
- cooldown still running since the last exit (default 5 minutes)
- daily trade cap reached (default 10)
- daily loss limit hit (default $1,000, measured as equity − last equity)
- an order is still working
- an untracked position exists, or the ledger and broker positions mismatch

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
| `BOT_DB_PATH` | `<repo>/stocks/trading_bot.db` | ledger location; also determines the lock file |
| `BOT_DRY_RUN` | `false` | evaluate signals, send no orders |
| `ALPACA_DATA_FEED` | `iex` | `iex` is free; `sip` needs a subscription |

## Dashboard

`http://127.0.0.1:5000` — polls `/api/metrics` every 5 seconds and shows equity,
today's P&L, realized P&L, win rate, expectancy and profit factor, the open
position with its live stop levels, an equity curve, a cumulative realized-P&L
curve, the trade log and the bot's event log. It flags a stale loop, an
unreachable broker, and an untracked position.

**It does not currently surface `position_mismatch`** — that state is only
visible via `python -m bot.runner --status` or by reading `bot_state`
directly. If you're relying on the dashboard alone to notice trouble, a
quantity/side mismatch will not show up there yet.

Credentials are stripped from the payload (`Settings.public_dict()`). The page
binds to localhost only; it has no authentication, so do not expose it.

## Tests

```bash
python -m unittest discover -s tests -v
```

77 tests (verified by running the suite) covering the indicators, every entry
and exit path, partial-fill P&L aggregation, position reconciliation
(quantity and side mismatches), single-instance locking, database migration,
position sizing, and the ledger's P&L arithmetic. They construct synthetic
bars and temporary databases, so they need no credentials and no network.

## Known limitations

These are gaps I could confirm in the current code, not just theoretical
concerns — see git history / review notes for how each was verified.

- **Ambiguous order submission.** `Broker.close_position()` and
  `Broker.submit_market_order()` can raise after Alpaca has already accepted
  the order but before the response reaches the client. The bot cannot
  distinguish "rejected" from "accepted, response lost." For exits, the
  fallback path can end up booking a trade at a **guessed** price rather than
  the order's true fill price. For entries, the bot is unlikely to submit a
  true duplicate (reconciliation catches a filled-but-unrecorded buy as an
  untracked position on the next cycle), but it still lands in a stuck state
  requiring `--adopt`.
- **No `client_order_id` recovery.** Fixing the above properly means
  generating and persisting an idempotency key *before* the network call, and
  querying Alpaca for it on an ambiguous failure rather than guessing. This
  has not been implemented; `close_position()`'s request type doesn't support
  a `client_order_id` at all, so exits would need to move onto
  `submit_market_order()` with an explicitly computed quantity.
- **Position-mismatch gate doesn't verify magnitude against recorded
  partials.** When a close order is in flight, any broker quantity smaller
  than expected is accepted as "explained by that order" without checking
  that the shortfall's size is consistent with what `exit_fills` has actually
  recorded. Confirmed by direct test: a scenario where only 2 of an actual
  9-share gap was accounted for still passed the gate while a `pending_exit`
  was set.
- **The single-instance lock is scoped to the database file, not to an
  account or symbol.** Two processes with different `BOT_DB_PATH` values can
  trade the identical Alpaca account and symbol with no protection from this
  mechanism at all.
- **`close_position()` closes the entire position, always.** There is no
  `qty`/`percentage` override anywhere in this codebase. If shares in the
  traded symbol exist outside the bot's own tracked quantity, closing the
  position sells all of them — this is exactly what the quantity-mismatch
  guard above exists to catch, but it's the underlying reason that guard is
  necessary rather than optional.
- **`position_mismatch` is not exposed on the web dashboard**, only via
  `--status` and the events log (see **Dashboard**, above).
- **The lock file (`<db_path>.lock`) is not covered by `.gitignore`.** The
  existing `*.db` / `*.db-wal` / `*.db-shm` patterns don't match the
  `.db.lock` suffix.

## Relationship to the notebooks

`trading_bot.ipynb` is the exploration that this package grew out of, and it
still holds the charting and data-inspection cells. It is not the runnable bot:
it targets `alpaca_trade_api`, which Alpaca has retired, and its trading logic
was never finished. Use `bot/` to trade and the notebook to explore.
