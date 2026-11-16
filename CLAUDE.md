# Operating Scribe

This file is for whoever drives this repository from a prompt. It is not a
tutorial: it records the things that cost real time or nearly caused harm, so
they are not rediscovered one mistake at a time.

Authority order when they disagree: `BUDGET_ARCHITECTURE.md` (the ADRs say
*why*) → `BUDGET_IMPLEMENTATION_PLAN.md` (the milestone contract) →
`ARCHITECTURE.md` → `DEVELOPMENT.md` → this file.

## Before touching the user's database

`data/app.sqlite3` holds real money records. Every change to it follows the
same three steps, in this order:

1. **Back up and verify.**

   ```bash
   uv run python -m app backup        # prints verified=True or fails
   uv run python -m app backup-list --json
   ```

   Never use `cp`: a plain copy of `app.sqlite3` omits the `-wal` file and
   hands back a *stale* database with no error at all. `backup` goes through
   SQLite's online backup API and verifies the copy for you.
2. **Rehearse on the copy.** Run the migration or script against the backup in
   a temporary directory with `SCRIBE_DATABASE_PATH` pointing at it. Check the
   result before going near the real file.
3. **Apply, then verify again.** Same checks, plus: the journal still balances
   to zero per currency, and no row count changed that should not have.

Keep the backup. `data/` is gitignored, so backups never enter version control.

To go back:

```bash
uv run python -m app restore <backup>                        # preview only
uv run python -m app restore <backup> --apply --confirm REPLACE
```

Stop both LaunchAgents first — every process that opens the database claims it,
so a scheduled sync blocks a restore just as the Web service does, and
`restore` refuses with no override while any claim is held. It
and it backs up whatever it replaces, so the step is itself reversible. It also
refuses a backup whose ledger does not balance, before touching anything.

## Stopping and starting the local service

It is a launchd agent with `KeepAlive`, so `kill` only makes launchd restart
it. It also holds the database open.

```bash
uv run python -m app service stop       # stop the generated user service
uv run python -m app service install    # regenerate it for this checkout
uv run python -m app service start      # start it again
```

Stop it before applying a migration. Restart it after any code change, or the
running process keeps serving the old templates and routes.

## Orientation, without guessing

```bash
uv run python -m app schema --table wallets   # never guess a column name
uv run python -m app schema                   # all mapped tables
uv run python -m app doctor --json            # config, database, Gmail health
uv run python -m app money balances --json    # exact minor units
```

Forty-one tables share very similar names. Ask `schema`; a guess against a
money schema is worse than a question.

## The daily loop, from the command line

Everything the web UI can write has a command, so driving the browser is never
required. The two steps that matter most:

```bash
uv run python -m app money status --json                  # what is waiting
uv run python -m app money transaction-confirm 1301 1303  # dry-run
uv run python -m app money transaction-confirm 1301 1303 --apply
uv run python -m app money allocate 22 --expected-version 1 \
  --client-token daily-1 --line 394:31:5                  # dry-run
uv run python -m app money allocate 22 ... --apply
```

A `--line` is `amount:wallet_id[:category_id]`, and the amount is written the
way it is shown. Anything that posts previews first: run it without `--apply`,
read what it says it will do, then repeat with `--apply`.

Anything that quotes a version reads it first:

```bash
uv run python -m app money event-show 22 --json        # version, status, note
uv run python -m app money transaction-show 1306 --json # updated_at, email_id
uv run python -m app money category-list                # version per category
```

`uv run python -m app money --help` lists the rest — cards, categories,
arrangement, reparse, notes, sync cancellation.

## Facts that are easy to get wrong

- **Migrations.** `alembic upgrade head` is refused on purpose — it will not
  guess a target. Use `uv run python -m app init` for the configured database,
  or set `SCRIBE_DATABASE_PATH` for a specific file.
- **Money is stored in minor units.** TWD is stored with two decimals and
  displayed with none, so `24000` is NT$240. Web forms take the amount the way
  it is written (`240`); the database keeps minor units. Do not mix the two.
- **The opening cutoff is a hard boundary.** Events at or before the applied
  cutover instant are historical evidence and can never post again. An empty
  page for such a month is correct, not broken.
- **Totals come from posted ledger lines.** A parsed Gmail transaction
  contributes nothing until it is confirmed *and* allocated. An empty "today"
  usually means a queue is waiting, not that nothing was spent.
- **Card matching never guesses.** A card is resolved by issuer plus last
  four. Registering a card does not backfill existing events; rerun
  `money intake-confirmed --apply` to reconcile them.

## Writing changes

Follow the milestone contract in `BUDGET_IMPLEMENTATION_PLAN.md`: implement
one milestone, run the full gate, record the decision as an ADR, update the
documents, make one reversible commit, and stop for review.

```bash
uv run ruff check . && uv run ruff format --check . && uv run mypy . && uv run pytest
```

Never report a check as passing without that command's actual output.

Prefer the application services over raw SQL, even in a throwaway script: they
enforce currency, active-account, optimistic-version, and idempotency rules
that hand-written SQL would bypass.

## Never

- Copy a real balance, account number, merchant, card number, or email body
  into source, tests, fixtures, logs, screenshots, or commit messages.
- Post a journal entry from an uncertain source transaction, mutate a posted
  entry instead of reversing it, or use `float` for money.
- Expose `/money`, `/budget`, `/allocations`, `/clearing`, `/subscriptions`,
  or `/cards` through the external hostname.
