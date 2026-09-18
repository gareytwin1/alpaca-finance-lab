# Operations runbook

Practical procedures for running this bot day to day. See `README.md` for how
the strategy and order lifecycle actually work — this document assumes you've
read that, and focuses on "what do I actually type when X happens."

Every command below is run from the `stocks/` directory with the same
environment variables the bot itself uses (`ALPACA_API_KEY`, etc.).

## Quick reference: what does the bot do on its own?

| Condition | Bot does | You need to |
|---|---|---|
| Normal operation | Trades automatically | Nothing |
| Untracked position | **Blocks all trading on the symbol** | `--adopt` or close manually |
| Position mismatch (qty/side) | **Blocks all trading on the symbol** | Investigate, resolve manually |
| Pending exit (order in flight) | Waits, retries next cycle | Usually nothing — resolves on its own |
| Exit unconfirmed (timeout, partial fill) | **Keeps ledger trade open**, retries next cycle | Usually nothing; check if it persists |
| Exit submission outcome unknown | Resolves it by client order id next cycle | Nothing — automatic recovery |
| Entry unconfirmed | Books nothing; next cycle reconciles | Usually nothing; check for an untracked position after |
| 401 / auth failure | **Cycle fails, no trade attempted, exception logged** | Fix credentials |
| 429 / rate limited | **Cycle fails**, retried next cycle | Usually nothing; investigate if constant |
| Broker unreachable (timeout, network) | **Cycle fails**, position/trade state untouched | Usually nothing; investigate if persistent |
| Second bot process started | **Refuses to start**, exits immediately | Confirm only one is meant to run |

"Blocks all trading on the symbol" means exactly that symbol — the guard is
per-symbol, and it stops *entries and management both* until you resolve it.

## Checking status

```bash
python -m bot.runner --status
```

Read-only — does not take the instance lock, does not touch the broker beyond
a few GET calls. Shows account equity, market state, the broker's live
position, the bot's tracked trade (if any), trades-today/cooldown, and any
`untracked_position` or `position_mismatch` warning.

Run this first whenever something looks wrong. It's also the *only* place
`position_mismatch` currently surfaces — the web dashboard does not show it
(see README → Known limitations).

## Starting the bot

```bash
python -m bot.runner
```

Runs until Ctrl-C (finishes the in-progress cycle, then stops cleanly). Logs
go to stdout and to `BOT_LOG_PATH` (default `app.log`).

Before starting for the first time, or after any doubt about state:

```bash
python -m bot.runner --status
```

Confirm the broker position and the bot's tracked trade agree before letting
it run unattended.

## Stopping the bot

`Ctrl-C` (SIGINT) or `kill <pid>` (SIGTERM) — both are caught and finish the
current cycle before exiting; they do not cancel any in-flight order. If you
need the position closed *before* stopping, use `--close` first (below), then
stop the loop.

`kill -9` / SIGKILL skips the graceful handler. This is safe with respect to
the ledger (see "Recovering after a crash," below) but skips writing the
"Bot stopped" log line.

## Running one cycle

```bash
python -m bot.runner --once
```

Same guards, same order logic, as the loop — just one pass. Useful for
testing a config change or nudging the bot after resolving a blocked state,
without leaving it running unattended.

## Dry-run mode

```bash
python -m bot.runner --dry-run          # loop
python -m bot.runner --dry-run --once   # single cycle
```

Evaluates signals and logs what it *would* do; never calls
`submit_market_order`. Confirmed by test
(`test_dry_run_never_touches_the_ledger`) — the ledger is never written to in
this mode. Use this to sanity-check a strategy or settings change before
trusting it with real (paper) orders.

## Checking logs

```bash
tail -f app.log                          # or $BOT_LOG_PATH if overridden
```

Every abnormal condition this bot detects is also written to the SQLite
`events` table (visible on the dashboard's event log, or directly):

```bash
sqlite3 trading_bot.db "SELECT ts, level, message FROM events ORDER BY id DESC LIMIT 20;"
```

## Running the tests

```bash
python -m unittest discover -s tests -v
```

86 tests as of this writing (confirm the current count by running it — don't
trust a stale number in a document). No credentials or network required; they
build synthetic bars and temporary databases.

## Handling an untracked position

**Bot behavior:** blocks all trading on the symbol. Does not touch the
position.

1. `python -m bot.runner --status` — confirm the broker position shown.
2. Decide: is this a position the bot should manage going forward, or
   something to handle outside the bot (a manual trade, leftover shares from
   before this bot existed)?
3. To adopt it: `python -m bot.runner --adopt`. This opens a ledger trade at
   the position's current average entry price and current high-water mark —
   it does **not** know the position's real original entry reason or time.
4. To leave it alone: close it yourself in the Alpaca UI/API. The bot will
   stop reporting it as untracked once the broker position is gone.

Do not `--adopt` a position you don't recognize without checking the account
activity in Alpaca first — adopting is not reversible from within the bot
(there's no "un-adopt"; you'd have to manually close the resulting trade row).

## Handling `position_mismatch`

**Bot behavior:** blocks all trading on the symbol — same as an untracked
position, but here the bot *does* have a tracked trade, and it disagrees with
the broker.

1. `python -m bot.runner --status` and read the specific message — it tells
   you whether the broker has more shares, fewer shares, or the wrong side.
2. **Side mismatch** (e.g. ledger long, broker short): this should not happen
   under normal operation — investigate account activity in Alpaca directly.
   This bot is long-only and has no logic for managing a short; do not expect
   `--close` to behave sensibly here without understanding how the account
   got into this state first.
3. **Broker has more shares than expected:** something (a manual buy, a
   corporate action) added shares to the same symbol. Closing the position
   would sell those too. Decide what those extra shares are before doing
   anything — if you close now via Alpaca directly, do it deliberately, not
   through this bot's `--close` (it closes the *entire* broker position, not
   just the bot's tracked portion).
4. **Broker has fewer shares than expected, no close order working:**
   something sold behind the bot without it being the bot's own close order
   (a manual sell, a broker-side stop that wasn't a bracket the bot tracks).
   Check Alpaca's order history for the symbol to find out what happened,
   then reconcile the ledger by hand if needed (see below).
5. There is currently no CLI command to force-clear a `position_mismatch`
   short of resolving the underlying discrepancy — the guard re-checks every
   cycle and clears itself once the broker and ledger agree again.

## Handling a pending exit

**Bot behavior:** waits and retries automatically. Usually requires nothing.

A `pending_exit` means the bot has already submitted a close order and is
waiting for it to settle. This is normal during the few seconds a market
order takes to fill. If it's still pending after several minutes:

1. `python -m bot.runner --status` and check the broker for that order id
   directly in the Alpaca dashboard.
2. If the order is genuinely stuck (e.g. `pending_cancel` for an unusually
   long time), that's an Alpaca-side issue — decide whether to intervene
   directly in Alpaca (cancel it there) rather than fighting the bot's own
   retry logic.

Every close order the bot sends carries a client order id of the form
`bot-exit-<trade_id>-<attempt>`, which is searchable in Alpaca's order
history. That's the fastest way to see exactly which orders the bot sent for
a given trade, and in what order.

## Handling an exit whose submission outcome is unknown

**Bot behavior: automatic recovery.** If the close request fails in a way
that leaves it unclear whether Alpaca accepted it (a lost response, a
timeout mid-request), the bot has already persisted the client order id
*before* sending, and reports `exit-unconfirmed` with status `unsubmitted`.
On the next cycle it asks Alpaca what exists under that id:

- **Order exists and filled** — booked normally, no duplicate sent.
- **Order exists and is working** — the bot waits for it.
- **Order exists but died** — any partial fill is preserved, and a
  replacement goes out under a *new* id.
- **No such order** — the submission genuinely never landed, so the bot
  retries under the *same* id.

You do not need to intervene for this case. If the id lookup itself keeps
failing (broker unreachable), the bot deliberately refuses to send another
close — the ledger trade stays open, the position is left alone, and it
retries once Alpaca is reachable. That's a wait, not a stuck state, but if
it persists you can search the client order id in Alpaca directly to see
the truth for yourself.

## Handling an uncertain entry

**Bot behavior:** books nothing to the ledger. If the order actually filled,
the *next* cycle's reconciliation will surface it as an untracked position
(see above) — the bot does not lose track of a filled entry, but it also
doesn't automatically resume managing it.

1. Wait one cycle, then `--status`.
2. If it shows an untracked position, that's your confirmation the entry
   filled — follow the untracked-position procedure above.
3. If the position is still flat, the order did not fill (or was truly
   rejected) — no action needed.

## Handling an Alpaca API outage

**Bot behavior depends on how you're running it.** In the loop
(`python -m bot.runner`), `run_forever()` catches any exception a cycle
raises, logs it, and retries on the next poll — it does **not** guess at
position or trade state, book a phantom exit, or arm the cooldown on a failed
cycle. Under `--once` (or `--close`), there is **no such wrapper**: an
exception from a broker call not already caught internally (account/clock/bar
lookups, mainly) will propagate all the way out and end the process with a
traceback and a non-zero exit, rather than a clean logged failure. The
specific broker call still logs what failed before it propagates, so the log
has the detail even though the process itself exits hard.

Nothing to do except wait, unless the outage is prolonged — in which case
treat it like any other extended broker downtime (no orders can be placed or
confirmed regardless of what this bot does).

## Handling a 401 (auth failure)

**Bot behavior:** never silently treated as "no position" or "order gone" —
`Broker.position()`/`get_order()` re-raise anything that isn't a genuine 404.
In the loop this means the cycle fails and is logged, repeating every
`BOT_POLL_SECONDS` until fixed (will spam the log/event table). Under
`--once`, it surfaces as an uncaught exception (see above).

1. Check `ALPACA_API_KEY` / `ALPACA_SECRET_KEY` are set and match the
   endpoint in `ALPACA_BASE_URL`.
2. Stop the bot while you fix credentials — there's no backoff on repeated
   401s in loop mode, so it will retry every `BOT_POLL_SECONDS`.

## Handling a 429 (rate limited)

**Bot behavior:** same shape as the outage/401 cases above — caught, logged,
and retried automatically in the loop; can surface as an uncaught exception
under `--once`. No special backoff is implemented beyond the normal poll
interval.

If this happens often, increase `BOT_POLL_SECONDS`, or check whether another
process (a second bot instance, a script, the dashboard polling too
aggressively) is sharing your API rate limit.

## Handling a partially filled exit

**Bot behavior:** automatic. The ledger trade stays open, the cycle reports
`exit-unconfirmed`, and the bot keeps checking the same order. If that order
later dies without fully filling, the bot resubmits a close for the remainder
and combines both fills' quantities and prices into the final ledger P&L —
see README → Partial-fill accounting.

Nothing to do unless it persists for an unusually long time — then treat it
like a stuck pending exit (above).

## Recovering after a bot crash or restart

**Bot behavior:** safe by design for the trade ledger itself — `close_trade`
is idempotent, and the current `pending_exit`/`exit_fills` state is persisted
to SQLite *before* the bot waits on a broker response, specifically so a
crash mid-wait doesn't lose the order id.

What is **not** guaranteed atomic with the trade-closing write: the cooldown
timestamp, clearing `pending_exit`, and the "EXIT" event-log line are each a
separate database write. A crash at exactly the wrong moment (after the trade
row is closed, before those follow-up writes) can leave the cooldown
un-armed, meaning the bot could enter a new position sooner than
`BOT_COOLDOWN_SECONDS` after the crash. This is a known, narrow gap — not
something to routinely worry about, but worth knowing if you ever see a
suspiciously-fast re-entry after a restart.

After any crash or unclean stop:

1. `python -m bot.runner --status` before restarting the loop.
2. Confirm the broker position and ledger trade agree.
3. If a `.lock` file was left behind because the process was killed hard
   enough to skip cleanup, a normal `flock` release happens automatically
   when the file descriptor closes with the process — you should not need to
   delete `trading_bot.db.lock` by hand. If the bot refuses to start claiming
   another instance is running and you've confirmed no process is actually
   running (`ps aux | grep bot.runner`), only then remove the stale lock
   file manually.

## Handling a stale working order

If `--status` or the "an order is still working" guard persists across many
cycles:

1. Check the order directly in Alpaca — is it actually still open, or is
   there a discrepancy between what Alpaca reports and what the bot expects?
2. If it's genuinely stuck, cancel it directly in Alpaca rather than waiting
   indefinitely — the bot will pick up the resulting state (canceled, or a
   position change) on its next cycle.

## Checking broker state before using `--adopt`

Always run these two checks before `--adopt`, since adopting is not
reversible from inside the bot:

```bash
python -m bot.runner --status                 # shows the broker position summary
```

Then check Alpaca directly (UI or API) for the position's actual order
history — confirm you recognize how those shares got there before handing
them to the bot's automated exit logic.

## Database backup before significant migrations

The bot auto-migrates the schema on startup (README → Database migration).
Before pulling in a code change that you know touches `storage.py`'s schema,
or if you're just being cautious:

```bash
cp trading_bot.db trading_bot.db.bak
```

WAL mode means a live copy while the bot is running can miss data still in
the `-wal` file — stop the bot first for a guaranteed-consistent backup:

```bash
# bot stopped
cp trading_bot.db trading_bot.db.bak
cp trading_bot.db-wal trading_bot.db-wal.bak 2>/dev/null || true
```

## Verifying only one bot runner is active

```bash
ps aux | grep "[b]ot.runner"
```

Or simply try to start a second instance against the same `BOT_DB_PATH` — the
lock will refuse it immediately with a clear error rather than letting it
proceed. This is a genuine guarantee for two processes sharing one database
file. **It is not a guarantee across different `BOT_DB_PATH` values** — two
processes configured with different database paths can still trade the same
Alpaca account and symbol simultaneously with no protection from this
mechanism. If you run more than one instance, confirm by hand that they are
never configured to trade the same symbol on the same account.

## Running on a schedule (cron)

The bot is scheduled via the system crontab, not as a long-lived
`bot.runner` loop. Each trading-hours minute, cron runs `bot.runner --once`
through `scripts/run_bot_once.sh`, which does one cycle and exits. This
avoids babysitting a persistent process across sleep/reboot/crash, and it
composes cleanly with `single_instance()`: if a cycle ever runs long, the
next minute's invocation just fails closed (exit 2, logged, no action)
instead of racing it.

```bash
crontab -l                    # see the installed schedule
tail -f cron.log              # wrapper-level output (missing env file, etc.)
tail -f app.log               # the bot's own log — this is the one that matters
```

Schedule: every minute, 08:30–15:00 **America/Chicago**, Monday–Friday —
which is 09:30–16:00 **America/New_York**, the regular session. The two
zones stay exactly 1 hour apart year-round because both observe US DST on
the same dates, so this mapping does not need revisiting at DST changeovers.
If the bot ever runs from a host in a different timezone, recompute the
cron hours rather than reusing them as-is.

Credentials live in `stocks/.env.alpaca` (mode `600`, gitignored, sourced
only by the wrapper script) because cron does not run your shell's rc files,
so `ALPACA_API_KEY`/`ALPACA_SECRET_KEY`/`ALPACA_BASE_URL` would otherwise be
unset. If you rotate keys, update that file — exporting them in your shell
again has no effect on cron.

**Stopping the schedule:**

```bash
crontab -e     # delete the bot's lines, or:
crontab -r     # removes ALL cron jobs for this user — only if the bot's
               # entries are the only ones present
```

Removing the crontab entries does not touch an open position — it only
stops new cycles from running. Check `--status` and close manually if
needed.

**A crontab-scheduled cycle uses the same code path as manual runs** —
`--once` takes the lock, runs `run_once()`, and every guard, reconciliation
rule, and confirmation check described elsewhere in this document applies
identically. There is nothing schedule-specific about order handling.

## What this document does not cover

Anything involving live (non-paper) trading — this bot refuses to start
against a non-paper endpoint, and this runbook does not attempt to describe
operating it as if that restriction were lifted.
