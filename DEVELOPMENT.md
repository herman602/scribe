# Development

The reviewed, milestone-by-milestone budget handoff contract is
[`BUDGET_IMPLEMENTATION_PLAN.md`](BUDGET_IMPLEMENTATION_PLAN.md). Implement and
review only one budget milestone at a time.

## Environment

```bash
uv sync --extra dev
uv run python -m app --help
```

Python 3.12 is the minimum supported version. The current development environment
may use a newer compatible Python.

## Public release and licensing

Original Scribe code and documentation are licensed under
`AGPL-3.0-or-later`. Vendored browser assets retain the upstream licenses listed
in [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md); keep their local license
texts beside the assets. A dependency being usable by Scribe does not make it
AGPL, and third-party notices must not imply otherwise.

Public contributions must follow [`CONTRIBUTING.md`](CONTRIBUTING.md), especially
the synthetic-fixture rule. Security reports follow
[`SECURITY.md`](SECURITY.md) and never attach credentials, databases, logs, real
messages, statements, or financial screenshots.

### Distribution rehearsal

Before publishing a release, build both artifacts and inspect every archive
member without extracting it:

```bash
UV_CACHE_DIR=/tmp/scribe-public-release-uv uv build
uv run python scripts/verify_distribution.py dist/*.whl dist/*.tar.gz
```

The verifier compares the artifact with every runtime file under `app`, checks
the license and third-party notices, rejects local configuration/databases and
credential-like content, bounds archive members, and refuses unsafe paths. Do
not weaken a rule to make an artifact pass; correct the package manifest or
remove the private input.

The wheel must also work when the checkout is absent from Python's import path.
Create an empty temporary directory and virtual environment, install only the
built wheel, then invoke:

```bash
TEMP_ROOT=/path/to/new-empty-temporary-directory
uv venv "$TEMP_ROOT/venv"
uv pip install --python "$TEMP_ROOT/venv/bin/python" dist/*.whl
"$TEMP_ROOT/venv/bin/python" scripts/smoke_installed_distribution.py \
  --workdir "$TEMP_ROOT/work" \
  --forbid-import-root "$PWD"
```

The smoke program discards inherited `SCRIBE_*` settings, initializes a new
database, verifies the installed migration head, accepts only the documented
missing-OAuth doctor failures, loads packaged templates/static files, and proves
backup plus restore against synthetic state. `--forbid-import-root` makes the
run fail if `app` came from the checkout instead of site-packages. Never point
`--workdir` at a real Scribe data directory.

## Quality gate

Every implementation milestone must run:

```bash
uv run ruff check .
uv run ruff format --check .
uv run mypy .
uv run pytest
```

Do not report a check as passing unless that exact command completed
successfully. Coverage is branch-aware with an initial 85% project threshold.

## Package boundaries

- `app/domain`: framework-independent business rules.
- `app/application`: commands, queries, and use-case orchestration.
- `app/infrastructure`: settings, logging, database, Gmail, parser, PDF, and
  secret-store adapters.
- `app/presentation`: Typer and FastAPI/Jinja2 adapters.
- `app/worker`: one non-blocking local task runner; durable ownership and
  progress remain in SQLite application repositories.

Routes and CLI functions validate/translate input and invoke application or
infrastructure lifecycle services. They do not issue SQL or Gmail API calls.

## Configuration tests

Tests pass settings explicitly or isolate the current directory. Never depend on
the developer's `.env`, `config.toml`, credentials, token, database, or mailbox.

External-report tests send an explicit configured Host header and prove that
only the overview, `/reports`, and packaged static assets are readable. When
external confirmation is enabled, tests require a cryptographically verified
Access assertion plus CSRF and same-origin checks for the one confirmation
route. All sensitive-detail and other mutation routes remain hidden. JWT tests
use generated anonymous RSA fixtures and never contact Cloudflare.

Validate generated macOS service definitions after changing their commands,
working directories, or paths:

```bash
uv run pytest -q tests/unit/test_macos_service_rules.py \
  tests/integration/test_macos_service_management.py tests/test_service_cli.py
```

The LaunchAgent is deliberately user-scoped. Do not convert Scribe to a root
LaunchDaemon or expose its listener publicly. A LAN listener is valid only with
`lan_access_enabled`, `host = "0.0.0.0"`, and exact private
`lan_trusted_hosts`; tests must prove unknown LAN Hosts and external routes stay
blocked. The generated scheduled-sync agent must use eight three-hour
`StartCalendarInterval` entries without `KeepAlive` or `RunAtLoad`; it invokes
the normal incremental command and relies on the SQLite sync lease for
cross-process exclusion.

## Test data

All future email/PDF fixtures use invented identities, addresses, amounts,
last-four values, and fixture-only passwords. Normal tests never access live
Gmail. Encrypted PDF fixtures must be reproducible and visually verified when
created.

## Gmail adapter tests

Normal tests inject fake OAuth flows, refreshers, Gmail resources, and HTTP
errors. They must never open a browser, bind an OAuth callback port, use a real
token, or contact Gmail. Fixture client IDs, secrets, access tokens, refresh
tokens, addresses, counters, and history IDs are invented.

OAuth health regression tests use the existing `app_settings` repository and a
fixed UTC clock. They must prove that explicit `auth` forces a browser consent
grant (including `prompt=consent`), day-six Testing reminders are deterministic,
refresh failure is durable before a `sync_run` exists, and successful consent
clears the failure. A test must never advance `authorized_at` merely by reusing
a still-valid saved token.

The Google discovery client is isolated in `infrastructure.gmail`; its dynamic,
untyped boundary uses narrow protocols and targeted mypy suppressions. Domain and
application modules do not import Google packages.

Processed-message labeling is covered by fake `labels.list`, `labels.create`,
and `messages.batchModify` resources. Tests must verify message-ID batching,
idempotent backfill, retryable outbox state, exact OAuth scope selection, and
suppression of history events caused solely by Scribe's own Label. Thread IDs
must never be passed to the Label adapter.

Milestone 4 tests enforce these synchronization invariants:

- `messages.list` is paginated and every persisted identity is a Gmail message
  ID, never a thread ID;
- sender-filtered hits expand `threads.get(format=metadata)` and inspect every
  underlying message;
- page items and the next-page checkpoint commit together;
- replay and rejected page-token restart remain idempotent;
- retry sleeps are injected, so tests never actually wait;
- full MIME, attachment-ID bodies, HTML fallback, Big5/CP950, malformed charset,
  byte limits, and per-message failure isolation use invented fixtures only.

No test should use a connected mailbox. Live smoke tests, if added later, must
be opt-in and use a dedicated non-personal account.

Milestone 5 tests additionally enforce:

- every parser is database-free and receives one Gmail message at a time;
- one message may emit multiple independently reconciled transactions;
- TWD/USD/JPY amounts use integer minor units and never `float`;
- marketing, missing amount/date, malformed encoding and HTML-derived text have
  explicit outcomes;
- correct/missing/wrong PDF passwords, unencrypted, malformed, oversized,
  timed-out, and image-only PDFs have sanitized statuses;
- decrypted/plain PDF bytes are not persisted; encrypted originals may be;
- parser runs and classification stages are auditable;
- reparsing refreshes parsed fields while preserving manual effective overrides.
- supplementary-card labels are explicitly keyed by Gmail account, institution,
  and four-digit card suffix; equal suffixes in another account or institution
  must not match;
- report card rankings group by institution key plus four-digit suffix and show
  the institution name, so equal suffixes at different banks remain separate;
- mailbox-derived 富邦/陽信/國泰/第一 layouts are represented only by invented
  values; Gmail thread grouping is never reproduced by treating a thread as one
  transaction;
- 永豐/華南 parser tests reproduce only the observed encrypted-PDF header and
  column order with invented values, or inject an explicit password-required
  status; committed tests never access the OS credential store.
- 渣打 fixtures use invented suffixes, amounts, dates, and authorization codes
  while preserving only the bilingual row order. Tests enforce one candidate
  per bilingual row, ignore footer refund wording, and reject login notices.

When a live mailbox is used to learn a new template, inspect a narrowly scoped
query, expand every shortlisted thread, and evaluate each Gmail message ID
independently. Record only field order, labels, optionality, and type rules in
code. Never copy names, addresses, card suffixes, amounts, merchants, message
IDs, attachment bytes, or passwords into fixtures, logs, commits, or review
notes.

Milestone 6 tests additionally enforce:

- `history.list` payloads are normalized into individual message additions,
  deletions, and label changes;
- duplicate events/pages collapse by Gmail message ID, with deletion taking
  precedence;
- history page tokens and latest history IDs checkpoint atomically;
- rejected saved page tokens restart from the committed cursor without duplicate
  work;
- unknown senders stop after metadata while accepted messages use the shared
  full-message persistence path;
- source deletion is soft and transaction/email evidence remains local;
- item failure leaves the committed cursor unchanged;
- a history 404 preserves the database and performs complete scoped
  reconciliation before establishing a new baseline.

Milestone 7 tests additionally enforce:

- dashboard totals never combine TWD, USD, or other currencies;
- display precision is currency-aware: TWD/JPY/KRW/VND omit fractional digits
  while storage continues to use each currency's configured minor-unit exponent;
- transaction filters remain bounded and SQL is confined to the read repository;
- HTMX requests return fragments while ordinary links/forms remain usable;
- retained Email text is escaped and stored HTML is never rendered;
- Host, Origin, and double-submit CSRF checks reject cross-site mutations;
- stale transaction review forms return a conflict instead of overwriting newer
  edits;
- explicit manual overrides survive an offline parser rerun while non-overridden
  effective fields refresh from the latest parser output;
- dashboard composition does not construct or contact a Gmail adapter.

Milestone 8 tests additionally enforce:

- spending trends include purchase, bill-payment, subscription, and fee while
  refund and money-movement types remain count-only;
- monetary totals, rankings, and trends never combine currencies;
- reports use effective override values and exclude ignored or superseded rows;
- daily boundaries use the configured IANA timezone and trends zero-fill every
  requested day/month;
- invalid report windows fall back to an allowlisted value;
- Chart.js and its MIT license are served locally without a Node build or CDN.

Chart.js 4.5.1 is pinned at
`app/presentation/web/static/vendor/chart.umd.min.js`. When intentionally
upgrading it, update the license and verify the vendored file:

```bash
shasum -a 256 app/presentation/web/static/vendor/chart.umd.min.js
```

The expected 4.5.1 SHA-256 is
`48444a82d4edcb5bec0f1965faacdde18d9c17db3063d042abada2f705c9f54a`.

Milestone 9 tests additionally enforce:

- SQLite lease acquisition is atomic and an unexpired owner cannot be stolen;
- an expired lease can be reclaimed, while release is owner-scoped;
- CLI and Web execution use the same lease guard;
- cancellation is persisted and observed only at safe page/message boundaries;
- the daemon-thread worker returns immediately and accepts at most one task;
- `/sync` reads durable historyId/counters/errors without constructing Gmail
  infrastructure, and its mutations retain Host/Origin/CSRF protection.

The Web worker is intentionally in-process and daemonized. A server shutdown can
stop it between checkpoints; tests therefore assert resumability, not
exactly-once remote execution. SQLite message IDs, unique constraints, and
cursor rules make replay idempotent. Do not use Uvicorn `--reload` or multiple
workers when manually testing background controls.

Regenerate the anonymous PDF fixtures only with:

```bash
uv run python tests/fixtures/generate_pdf_fixtures.py
```

The fixture password is test-only and unrelated to any identity. Render newly
generated PDFs with Poppler (`pdftoppm`) when available and inspect the page
image before committing.

## Budget Milestone B1 tests

The ledger foundation adds `tests/unit`, `tests/repositories`,
`tests/integration`, and `tests/migrations` packages. Their tests enforce:

- money is an integer minor-unit amount plus an ISO 4217 code; `float` and
  `bool` amounts are rejected at the domain boundary;
- a journal entry balances independently for each currency, and a draft that
  mixes TWD and JPY is rejected unless both net to zero;
- wallet totals equal their parent asset account's calculated ledger balance
  after an opening position and after an internal wallet reclassification;
- an edited accounts-sheet cell re-divides one account and leaves its total
  alone: a counterpart in another account and a balance the page never rendered
  are both refused, retyping the current number posts nothing, and resubmitting
  one edit moves the money exactly once;
- a stored wallet arrangement names every wallet of its account exactly once,
  resolves a repeated position against the order already on screen, posts
  nothing, and leaves a later wallet at the end of the column;
- the same arrangement rule orders the sheet's account columns, a later account
  joins the end of the sheet, and the sheet needs no horizontal scrollbar;
- a salary plan sheet writes only the amounts that changed, refuses a typed
  amount for a calculated destination, and reports a stale version as a
  conflict rather than overwriting it;
- an empty day names the queues blocking it and their sizes, a quiet day keeps
  its plain message, and a month before the opening cutoff says it needs
  nothing;
- every money form takes the written amount, carries its own currency, and
  accepts zero only where zero is a real plan (buffer, subscription price,
  manual salary line);
- a card liability is never listed among the accounts that hold money, and an
  asset account is never listed among the liabilities;
- read-only commands emit the same values as JSON that their text form prints,
  through one shared encoder, `schema` describes the mapped tables without
  needing a database, and `money status` reads local state without writing any
  of it;
- every web write has a command that calls the same application service, and
  the commands that post preview unless `--apply` is given;
- the balances read and the accounts sheet order the same accounts and wallets
  identically, because both read the stored arrangement;
- a read reports the optimistic token its write demands, so every command that
  takes an expected version can be reached from another command's output;
- a backup is verified against its source and captures writes still in the
  write-ahead log; a restore previews, keeps a rollback copy, and refuses both
  an unsound backup and a missing confirmation;
- a restore proves balance, integrity, and revision before touching the live
  file, stages and atomically replaces it, and refuses — unforceably — unless
  it can take the database's exclusive advisory lock, which every reader holds
  shared from `create_database` until its engine is disposed, and which an
  online migration takes for itself because Alembic builds its own engine;
- a refused restore leaves the live write-ahead log untouched, and only a
  successful one clears the sidecars of the file it replaced;
- an amount is parsed with the currency of the wallet, plan, or run that holds
  it, never with a configured default or a hidden input;
- a repeated idempotency key returns the existing entry instead of posting
  again, and an entry can be reversed exactly once;
- an idempotency key reused for different work — a different amount, wallet,
  entry type, or effective instant — raises `IdempotencyConflictError` instead
  of returning the earlier entry; reordered postings and a changed description
  still count as the same request;
- an unrelated entry occupying `reversal:journal_entry:{id}` makes `reverse`
  fail loudly rather than report a reversal that never happened;
- `post_balanced` cannot create a reversal at all: it has no `reversal_of_id`
  parameter, and an entry typed `reversal` without a target is rejected;
- a reversal's postings must be the exact opposite of the reversed entry, and a
  reversal cannot itself be reversed;
- a missing, inactive, foreign, or superfluous wallet, a currency mismatch, and
  an unknown ledger account all roll back every write, including the
  opening-equity account created in the same transaction;
- upgrading a `0004` database to `0005` preserves every Email and transaction
  row, keeps foreign keys enabled, and reports revision `0005`;
- downgrading to `0004` removes only the five ledger tables.

Ledger fixtures use invented institution keys such as `test_bank`, invented
display names, and invented amounts. Never commit a real balance, account
number, or masked reference.

## Budget Milestone B2 tests

The cards and source-event bridge adds `tests/unit/test_event_matching.py`,
`tests/web/`, and `tests/integration/test_financial_event_intake.py`. Their
tests enforce:

- `financial_events.amount_minor` is a positive integer magnitude; `float`,
  `bool`, zero, and negative values are rejected in the domain and by a
  database check constraint;
- one parsed transaction can own at most one `confirmed` source link, enforced
  by a partial unique index as well as by intake;
- unconfirmed, ignored, superseded, and deleted-source transactions are never
  staged, and each refusal carries a stable `IntakeSkipReason` code;
- a transaction with an `unknown` type, or without an amount, currency, or
  occurred-at value, is skipped rather than forced into an `adjustment` event;
- an unresolved, unconfigured, or ambiguous card mapping still stages the event
  and records `card_unresolved`, `card_not_configured`, or `card_ambiguous`;
- confirmation and intake share one Unit of Work: a failing intake rolls the
  confirmation back, leaving no event and no confirmed flag;
- a parser rerun refreshes `draft`, `needs_allocation`, and `ready` events, but
  a `posted`, `reversed`, or `ignored` event is never modified and instead
  records `SourceDiscrepancy` codes on its confirmed link;
- Gmail thread identity never participates in event uniqueness;
- the match scorer's gates and points are exact, and a score can only ever
  produce a `candidate` link;
- card readiness requires a `card_liability` liability account, an asset
  settlement account, an optional funding wallet on an active asset account,
  one shared currency, and a four-digit last four;
- `supplementary_card_settings` backfill is idempotent and never overwrites a
  card the user edited;
- upgrading `0005` to `0006` preserves every journal entry, posting, index, UTC
  value, and the `journal_postings` foreign key, and downgrading removes only
  the B2 tables, the `financial_event_id` column, and the pairing triggers;
- `/cards`, `/money/review`, and every `/budget` route return `404` for the
  external hostname while remaining available locally;
- a foreign event occupying an intake key, or a Gmail event whose confirmed
  link disappeared, raises `IdempotencyConflictError` instead of reporting a
  successful skip;
- a legitimate intake retry reconciles: an amount edited after confirmation
  updates the unposted event, and a card configured later remaps the event and
  clears `card_not_configured`;
- unconfirming or ignoring a bridged transaction records
  `source_no_longer_confirmed`;
- a full or reconciliation sync reparse reconciles bridged events, not only
  incremental sync and manual reparse;
- revision `0006` refuses to upgrade when a pre-existing journal row already
  violates the reversal pairing invariant;
- `intake.backfill(dry_run=False)` and `money intake-confirmed --apply` persist
  a reconciliation that stages nothing new, verified by reopening the database
  in a fresh Unit of Work. These tests deliberately drive the real path instead
  of a helper that always commits, because the defect lived in that path's
  commit condition; a dry run still persists nothing.

Budget Milestone B3B-2 adds the seven B3 acceptance tests and the cross-row
refund rule:

- a purchase posts its expense exactly once, and a resubmitted form replays the
  first entry instead of posting a second;
- a split must total the event exactly and use one currency;
- a card expense raises the card liability and reserves funding, and leaves
  every bank balance untouched;
- cash or direct debit reduces only the wallets that were selected;
- budget totals include refunds with the correct sign, so a purchase and its
  partial refund net out in the original category;
- a month close is previewable, is idempotent, cannot sweep more than the
  wallet has available, and will not move money between accounts;
- the buffer is reported beside the target, is not part of the spendable
  target, and is never swept;
- a refund may not refund another refund. That rule is cross-row, so the
  row-level `0007` constraint cannot express it and the allocation service
  enforces it on both the draft and the posting path;
- a partial refund is spread with largest-remainder distribution, so the shares
  sum back exactly and the result never depends on row ordering;
- spending belongs to the local calendar month it happened in: 23:30 on 31
  August in Taipei counts as August even though it is 15:30 UTC.

The first B3B-2 review added:

- only spending event types may be allocated; a deposit, withdrawal, transfer,
  interest payment, or adjustment is refused rather than posted as spending;
- two partial refunds cannot together exceed the expense they refund, and no
  refund may credit a wallet-and-category share the original never used;
- a refund requires an original that is posted and allocated, on the same card;
- a funding wallet must hold the event's currency even when the request
  bypasses the form;
- a buffer already consumed by overspending is not withheld again at month end,
  and both the buffer target and the remaining buffer are displayed;
- spending with no target, and spending with no category, are both reported and
  counted, including in a month that has no budget period yet;
- the month-end sweep is posted as `budget_close`;
- a target can be corrected from the page and from the CLI, each carrying the
  version it saw, and a stale version is a conflict.

The second B3B-2 review added:

- a refund must use the payment method of the event it refunds, so a card
  purchase cannot be refunded as a bank credit that leaves the card liability
  untouched while still releasing the card's reservation;
- a refund releases only the `(wallet, category)` shares it actually returned
  money to, matched back through `event_allocation_id`, instead of spreading
  the release pro-rata across every outstanding reservation.

Each of those protections was mutation-verified by reverting it and confirming
the matching test fails.

`IntakeResult.reconciled` counts the already staged events an apply run passed
through reconciliation, not the ones whose fields changed, and a dry run always
reports zero. Keep that meaning unless the counts are deliberately split into
would-reconcile and updated.

Ledger and card fixtures use invented institution keys, display names, last
four values, and amounts. Never commit a real balance, account number, card
suffix, or merchant.

Scratch ledger setup for local development:

```bash
uv run python -m app init
uv run python -m app money account-add --institution test_bank \
  --name "測試支票帳戶" --type checking --currency TWD
uv run python -m app money wallet-add --account-id 1 --name "房租"
uv run python -m app money opening-balance --account-id 1 \
  --amount-minor 500000 --observed-at 2026-08-07T21:30 \
  --split "尚未分配=300000" --split "房租=200000"
uv run python -m app money balances
```

Amounts are always integer minor units. `--observed-at` accepts an ISO 8601
value; a value without an offset is interpreted in the configured
`local_timezone` and stored as UTC.
Opening wallet shares are signed: an overdrawn envelope may be negative, and a
zero share configures the wallet without producing a zero-value posting. The
signed shares must still total the reconciled account balance exactly.

Scratch card and intake setup:

```bash
uv run python -m app money cards-backfill          # preview
uv run python -m app money cards-backfill --apply
uv run python -m app money intake-confirmed        # preview
uv run python -m app money intake-confirmed --apply
```

Both commands preview by default and print a stable reason code for every
transaction they refuse to stage. Intake writes no journal entry.

## Budget Milestone B4 tests

The subscription register adds `tests/unit/test_subscription_requirements.py`,
`tests/repositories/test_subscription_repositories.py`,
`tests/integration/test_subscription_requirement.py`,
`tests/integration/test_subscription_cli.py`,
`tests/web/test_subscription_routes.py`, and
`tests/migrations/test_subscription_migrations.py`. Their tests enforce:

- a monthly subscription contributes the month's effective price, and a yearly
  or custom cycle is spread so the months of one complete cycle sum to exactly
  the cycle price; the remainder always lands on the earliest cycle positions;
- a price may be 0 but never negative, and `float`, `bool`, and text amounts
  are rejected at the domain boundary and by a database check;
- TWD, JPY, and USD keep their own minor units and are never converted or
  added together; a `SubscriptionRequirement` refuses to hold two currencies;
- a yearly cycle is anchored by its billing month and a custom cycle by its
  active-from month, both across a year boundary and for months before the
  anchor; a cadence without its anchor is refused instead of guessed;
- activity and price ranges are half-open, so adjacent ranges are legal and a
  boundary month belongs to exactly one range;
- a historical month uses the price effective then, not the latest one, and
  changing a price never moves the cycle anchor;
- a deactivated or zero-price subscription still appears with its evidence and
  a stable reason code, and months before the end month keep the requirement
  they had;
- subscription and price updates are conditional `UPDATE ... WHERE version`
  statements, so a stale version is a conflict rather than an overwrite;
- an overlapping insert or update fails atomically, proven against the
  database triggers as well as the service, while two subscriptions may share
  one range;
- the database refuses every invalid cadence shape, billing month, billing
  day, mid-month boundary, malformed date, and inverted range;
- every B4 foreign key is `RESTRICT`, and a referenced category or wallet
  cannot be deleted;
- the default funding wallet must be an active wallet in an active asset
  account, is read from `app_settings`, and setting it creates no commitment
  and moves no balance;
- upgrading `0007` to `0008` preserves every Email, transaction, ledger, and
  B3 row, keeps `integrity_check` at `ok` and `foreign_key_check` empty, and
  downgrading removes only the two tables and their triggers; upgrade,
  downgrade, and re-upgrade produce identical schema objects;
- a migration failure and an orphan row each roll back schema, data, and
  revision together, and the next connection still reports `foreign_keys=1`;
- `/subscriptions` and every subscription write return `404` for the external
  hostname while remaining available locally, and writes keep the existing
  CSRF cookie and origin validation.

The first B4 review added:

- a subscription that already has a price cannot change its currency,
  cadence, cadence months, billing month, category, or funding wallet, and a
  custom cadence cannot move its anchor; each attempt is refused and the
  historical month is asserted byte-for-byte unchanged afterwards;
- the same subscription can still be renamed, re-dated, and given a different
  billing day without changing any month's amount;
- an ordinary edit can never restart a stopped subscription, and a
  deactivated row cannot clear the month it stopped;
- resuming creates a new interval: the stopped row keeps its end month, the
  gap between the intervals still contributes zero from both rows, and the new
  interval starts contributing only from its own month;
- resuming earlier than the month the previous interval ended is refused, as
  is resuming a subscription that never stopped, and deactivating twice;
- a duplicate active name is a stable `SubscriptionError` from the repository
  on both insert and update, never a leaked `IntegrityError`; the CLI prints a
  short message, the web layer returns `422`, and nothing is written.

The second B4 review added:

- no two intervals of one service may claim the same month, proven for
  create, edit, deactivate, and resume, and asserted through the requirement
  so no month ever emits two contributing items for one service;
- a resume is compared against every interval the service has held, so
  resuming an older interval into months a later one already covers is
  refused;
- renaming an interval onto a service it would overlap, and extending one
  interval into the next, are both refused;
- adjacency stays legal, an unbounded end blocks every later interval, an
  unbounded start blocks every earlier one, and two different services never
  constrain each other;
- an inactive interval stays editable while a later interval holds the same
  name, which the removed active-name index had made impossible;
- revision `0009` drops that index, adds the interval triggers, preserves
  every existing row, and round-trips back to `0008` and forward again.

The third B4 review added:

- revision `0009` is left exactly as published and revision `0010` carries the
  correction, because a database that already ran `0009` would otherwise have
  no migration path to the fixed schema;
- `0010` runs from both shapes a database at `0009` can have: it creates the
  active-name index when `0009` dropped it, and leaves it alone after
  verifying its definition when it is already present; an index of that name
  it did not install is refused;
- `0010` refuses to upgrade over rows that already break either invariant —
  overlapping intervals, or two intervals of one name running at once — and
  the error names row ids only, never a subscription name;
- an illegal `0008` database upgrading to head fails inside `0010` and rolls
  all the way back to `0008`, since every pending migration shares one
  transaction;
- `0010`'s downgrade returns the schema `0009` was published with;
- `0008 -> 0010 -> 0008 -> 0010` with real subscription rows preserves the
  full row snapshot, the indexes, the triggers, and the revision, so no state
  `0010` can hold is inexpressible at `0008`;
- the unique index on the active name and the interval triggers are both
  enforced by the database and neither covers the other: an overlapping insert
  or update trips the trigger, while an adjacent but also-running interval
  trips the index;
- the service enforces both rules too, so a second running interval of one
  name is refused with a stable message rather than reaching the index, while
  a stopped interval may still take the name of a running one.

Subscription fixtures are invented: `測試影音`, `測試雲端`, `測試日本影音`,
`test_bank`, and invented amounts. Never commit a real subscription name,
price, account, or card suffix.

Scratch subscription setup for local development:

```bash
uv run python -m app money subscription-default-wallet-set --wallet-id 1
uv run python -m app money subscription-add "測試影音" \
  --category-id 1 --funding-wallet-id 1 --cadence monthly
uv run python -m app money subscription-price-add 1 \
  --amount-minor 39000 --effective-from 2026-01
uv run python -m app money subscription-list
uv run python -m app money subscription-requirement --period 2026-08
uv run python -m app money subscription-deactivate 1 \
  --expected-version 1 --effective-from 2026-08
uv run python -m app money subscription-resume 1 --active-from 2026-11
```

Months are written as `yyyy-mm`; every boundary is a whole month, because B4
does not prorate by day. Editing an existing subscription or price requires
`--expected-version`, which `subscription-list` prints.

A service may hold several intervals over time, and they may not overlap:
adjacent is fine, sharing a month is not, and an interval with no end month
leaves no room for a later one. Once a subscription has a price, its currency,
cadence, anchor, category, and funding wallet are fixed, because the price row
does not record which of them it was entered under. Change one by stopping the subscription from a month and
adding a new one. `subscription-resume` does exactly that for a stopped
subscription: it creates a new interval and leaves the old row's end month in
place, so the months in between keep contributing nothing.

## Budget Milestone B5 tests

The salary allocation adds `tests/unit/test_salary_rules.py`,
`tests/repositories/test_salary_repositories.py`,
`tests/integration/test_salary_allocation.py`,
`tests/integration/test_salary_cli.py`,
`tests/web/test_salary_routes.py`, and
`tests/migrations/test_salary_migrations.py`. The unit module is named for the
rules rather than the milestone because mypy refuses two test modules that
share a basename. Their tests enforce:

- every salary amount is integer minor units: `bool`, `float`, text, and a
  negative value are refused at the domain boundary, a fixed or manual line may
  be 0 but never negative, and a salary itself must be positive;
- a fixed line carries its amount and a calculated or manual line must not, so
  a stale number can never become what a month freezes;
- the planned total and the remainder are exact and always sum back to the
  salary, and a plan larger than the salary is refused rather than clipped;
- the fulfillment path is decided from `financial_account_id`: two accounts at
  one institution are an `account_transfer`, and one account under two
  different display names is still `reserve_only`;
- every run and run-item status transition is enumerated, and every illegal one
  is refused;
- a salary event is judged by the shape of its posting — posted, a deposit, the
  right currency, its whole amount in that account's `Unallocated` wallet,
  against income and nothing else, not already used — and never by a merchant,
  an Email subject, or a bank name; an opening-equity credit, an internal
  transfer, an adjustment, a partial deposit, and a split across two accounts
  each name their own stable reason;
- both evidence documents round-trip through canonical JSON with every amount
  still an `int`, zero contributions and their reason codes survive, and a
  document whose amount is a float, a flag, or text is refused;
- recording a salary posts one balanced `salary_receipt` entry, raises the
  account exactly once, marks the event posted, and links the entry; the same
  token replays the first entry while a different amount, time, or account is a
  conflict; a failure leaves no event, income ledger, or entry behind;
- generation covers all three modes, refuses a missing or unexpected manual
  amount, maps subscriptions by funding wallet, refuses a positive requirement
  no line claims, keeps zero contributions in the snapshot, and writes nothing
  at all when the plan exceeds the salary;
- editing the template or a subscription price afterwards leaves an existing
  run byte-for-byte unchanged, and generating again replays the live draft
  instead of recalculating it;
- regenerating cancels the old draft, keeps it and its items, and writes
  generation + 1; a failed regeneration leaves the old draft untouched; an
  approved run is never regenerated; a closed month and a salary already used
  by another run are both refused;
- approving a same-account plan leaves the bank total unchanged while moving
  the wallets, and a cross-account plan changes neither ledger balance but does
  change reserved and available; a mixed run becomes `partially_funded`, an
  all-same-account run `funded`, an all-cross-account run `approved`, and a
  plan of only zero lines `funded` with no journal entry at all;
- approval is refused when the source wallet's available balance no longer
  covers the plan, and the whole transaction rolls back; a retry posts and
  reserves nothing further; a stale version changes nothing; no
  `scheduled_transfers` table is created;
- confirming a transfer moves both banks exactly once, posts a balanced
  `funding_transfer`, releases the waiting reservation, and never counts the
  amount twice — the balance and the reservation fall by the same amount, so
  available is unchanged; the last confirmation completes the run;
- a retry returns the first entry even when it names a different time, a
  non-waiting line has nothing to confirm, an inactive destination wallet rolls
  the whole thing back, and an `Asia/Taipei` 18:30 stays 10:30 UTC — there is no
  06:00 rounding anywhere;
- `reserved_minor` counts open B3 funding commitments *and* waiting B5 lines,
  each exactly once, and counts nothing for a draft, allocated, transferred, or
  non-reserving run;
- every template, item, run, and item update is a conditional
  `UPDATE ... WHERE version` (and, for a status change, `AND status`), so a
  stale writer is a conflict rather than an overwrite; frozen snapshot columns
  are refused by database triggers as well;
- a template and month hold one live run, a salary event feeds one live run, a
  cancelled generation is kept and the next one can be created, and one journal
  entry belongs to one line;
- a salary idempotency token is re-verified in full before any replay: an
  unrelated event holding the token is a conflict, as is a changed origin,
  event type, payment method, recognition basis, or status, a changed entry
  type, effective time, or event link, a different income account, an extra
  posting, a wallet or category that should not be there, and an amount given
  as `True` or a float that happens to equal the stored integer; a legitimate
  retry still replays the first event and entry;
- a regeneration is identified by provenance *and* payload: the same request
  replays, a different manual amount is a conflict, a draft cancelled by hand
  and replaced through the ordinary generate path is never reported as this
  request's result, another month's regeneration is never confused with it,
  and a failed regeneration records no provenance and leaves the draft alone;
- upgrading `0011` to `0012` keeps every salary row, index, and trigger, round
  trips back and forward, refuses to downgrade once a generation records a
  predecessor, and enforces the self-link, uniqueness, and pairing rules in
  the database as well as the service;
- upgrading `0010` to `0011` preserves every existing row, keeps B4's triggers
  and indexes, adds exactly `salary_receipt` and `funding_transfer` to the
  registry, and keeps `integrity_check` at `ok` with `foreign_key_check` empty;
- `0011` round-trips to `0010` and back on a database with no salary rows, and
  refuses to downgrade once salary rows or a posted salary entry type exist,
  reporting counts only and leaving revision, schema, and data unchanged;
- a migration failure and an orphan row each roll back schema, data, and
  revision together, and the next connection still reports `foreign_keys=1`;
- `alembic check` reports no difference between head and the models;
- `/allocations`, `/allocations/templates`, and every salary write return `404`
  for the external hostname, while locally a stale form is `409`, a domain
  failure is `422`, a successful write redirects, and the page renders the
  frozen snapshot rather than recalculating it after a price change.

Nineteen mutations were applied one at a time and each was confirmed to make
the suite fail: removing the planned-versus-salary guard, recalculating a
historical run live, crediting the destination bank at approval, letting an
approval or a transfer retry post twice, dropping or widening the waiting-line
term in `reserved_minor`, changing a same-account bank total, defaulting a
missing manual amount to zero, removing the available-funds check, making the
template update unconditional, letting a regeneration overwrite the run it
replaces, forcing 06:00 on a confirmed transfer, accepting a reused salary
token for different money or for an event that is not a salary, skipping the
replay verification entirely, comparing an amount before validating that it is
an integer, replaying a regeneration by month instead of by provenance, and
ignoring the request fingerprint.

Salary fixtures are invented: `測試薪資範本`, `測試薪轉帳戶`, `測試訂閱錢包`,
`test_bank_a`, `test_bank_b`, and invented amounts. Never commit a real
employer, salary, bank, account, wallet, or template name.

Scratch salary setup for local development:

```bash
uv run python -m app money salary-template-add "測試薪資範本" \
  --source-account-id 1 --currency TWD
uv run python -m app money salary-template-item-add 1 --name "測試生活費" \
  --destination-wallet-id 2 --mode fixed --fixed-amount-minor 300000
uv run python -m app money salary-template-list
uv run python -m app money salary-receipt --template-id 1 \
  --amount-minor 800000 --occurred-at 2026-08-05T09:00 --token scratch-1
uv run python -m app money salary-generate 2026-08 \
  --template-id 1 --salary-event-id 1
uv run python -m app money salary-show 1
uv run python -m app money salary-approve 1 --expected-version 1
uv run python -m app money salary-mark-transferred 2 \
  --expected-version 2 --effective-at 2026-08-06T18:30
```

Every amount on the command line is integer minor units, and every update needs
the `--expected-version` that `salary-template-list` or `salary-show` printed.
A cross-account line only becomes real money when you confirm the transfer;
Scribe never assumes a bank moved anything, and the 06:00 policy belongs to the
clearing milestone, not here.

## Budget Milestone B6 tests

Revision `0013_scheduled_transfers` adds the clearing hub. Its tests live in
seven files and each one owns a different question:

| File | Question it answers |
| --- | --- |
| `tests/unit/test_scheduled_transfer_rules.py` | Does the pure arithmetic hold — due-time resolution, the state machine, `derive_commitment_status`, wallet debits, backoff, and the reservation formula? |
| `tests/repositories/test_scheduled_transfer_repositories.py` | Does the SQL agree with the pure rule, and do the triggers and the conditional claim really refuse what they claim to? |
| `tests/migrations/test_scheduled_transfer_migrations.py` | Does `0012 ↔ 0013` round-trip, preserve every row, and refuse to downgrade over real history without changing anything? |
| `tests/integration/test_scheduled_transfers.py` | Does the whole lifecycle behave — reserve, execute once, catch up late, fail, retry, cancel, reverse, and survive a refund? |
| `tests/integration/test_scheduled_transfer_cli.py` | Do the `money` commands work, is `run-due` a dry run by default, and does every command dispose its engine? |
| `tests/web/test_clearing_routes.py` | Is `/clearing` local-only, CSRF- and origin-checked, paginated, and free of any bank-confirmation wording? |
| `tests/worker/test_maintenance_coordinator.py` | Does the coordinator start, stop, stay idempotent, and survive a failing pass? |

Three properties are easy to get subtly wrong and each has a test that fails if
the code stops holding them:

- **A commitment and the schedule that funds it are one reservation.** The
  repository test computes the expected number through the pure
  `wallet_reserved_minor` rule and compares it with the SQL on every
  arrangement, so the query and the documented formula cannot drift apart.
- **A failed schedule keeps its money.** For a fully linked schedule the
  coverage term and the debit term cancel out, so the interesting case is an
  autopay with an *unlinked residual*; that is what
  `test_a_failed_autopay_still_holds_back_its_unlinked_residual` pins down.
- **The execution key is what prevents a double posting.** The crash test
  proves the transaction rolls back;
  `test_the_execution_key_makes_a_second_posting_a_replay` proves a second post
  under the same key returns the first entry rather than writing another.

A note for anyone repeating the mutation verification: rewrite one source file
per mutation and Python's bytecode cache can betray you. A `.pyc` is validated
against the source's mtime and size at one-second granularity, so two mutations
of the same file that remove the same number of bytes inside the same second
look identical to the cache and the second run silently imports the first run's
bytecode. Purge `__pycache__` and set `PYTHONDONTWRITEBYTECODE=1` around each
run, otherwise a caught mutation can report itself as survived.

Clearing fixtures are invented: `test_bank_a`, `test_bank_b`, `測試帳單暫存`,
`測試卡`, last four `4321`, and invented amounts. Never commit a real bank,
account, wallet, card, or amount.

Scratch clearing setup for local development:

```bash
uv run python -m app money clearing-show
uv run python -m app money clearing-schedule \
  --source-account-id 2 --source-wallet-id 4 \
  --destination-account-id 1 --destination-wallet-id 3 \
  --amount-minor 8000 --date 2026-09-01 --client-token scratch-1 \
  --cover 1:8000
uv run python -m app money run-due
uv run python -m app money run-due --apply
```

`run-due` writes nothing without `--apply`, and a schedule whose time has
already passed needs `--allow-overdue` so a mistyped date cannot execute
immediately by accident.

## Budget Milestone B7 tests

Revision `0014_reconciliation` adds daily spending, manual expenses, bank
movements, source matching, and reconciliation. Its tests are split by question:

| File | Question it answers |
| --- | --- |
| `tests/unit/test_display_amounts.py` | Does a typed amount become exact minor units, and are scientific notation and over-precise decimals refused? |
| `tests/unit/test_b7_rules.py` | Do the pure rules hold — local day bounds, each bank movement's shape, reversibility, the signed difference, and the idempotency keys? |
| `tests/repositories/test_reconciliation_repositories.py` | Does the SQL agree — immutable snapshots, the as-of balance, the conditional `mark_reversed`, the bounded candidate query, and an upsert that never reopens a decision? |
| `tests/migrations/test_reconciliation_migrations.py` | Does `0013 ↔ 0014` round-trip over realistic B4/B5/B6 rows, keep every trigger and index, and refuse to downgrade over real evidence without changing anything? |
| `tests/integration/test_manual_expense.py` | Does a cash or card expense post atomically, replay, conflict, reverse, and edit or clear its note without rewriting accounting evidence? |
| `tests/integration/test_bank_movements.py` | Does each movement keep its own posting shape, does a replay compare where the money actually went, and do the B3 refund invariants still hold? |
| `tests/integration/test_source_matching.py` | Does matching run before intake, link only an unambiguous maximum, stay pending otherwise, and survive a parser rerun? |
| `tests/integration/test_daily_activity.py` | Are the totals ledger-derived, local-day bounded, currency-separated, and free of transfers and deposits? |
| `tests/integration/test_reconciliation.py` | Is the difference as-of, signed, stale-safe, refused when already zero, restricted to accounts an adjustment could settle, and bound to the snapshot its token names? |
| `tests/integration/test_spending_cli.py` | Do the thirteen `money` commands work, and do they fail with a stable message rather than a traceback? |
| `tests/web/test_spending_routes.py` | Are all the `/money/*` routes local-only, CSRF- and origin-checked, escaping, and built as cards rather than a wide table? |

The post-0015 consumed-cash follow-up adds no schema. Its regression cases live
in the B7 rule, bank-movement, CLI, and web suites: an ATM withdrawal marked as
fully consumed must debit a checking/savings wallet, create exactly one expense
allocation, appear as cash in daily and monthly totals, leave the cash account
unchanged, replay safely, and refuse a cash-account source.

Revision `0015_dedicated_wallet_spending` adds immutable per-allocation routing
evidence for purpose-wallet card purchases. Presence means the allocation is
excluded from daily/monthly allowance aggregates and reclassified into one
same-account clearing hub; absence preserves the existing monthly-budget path.
`tests/migrations/test_dedicated_wallet_migration.py` covers empty round-trip,
RESTRICT foreign keys, row-level immutability, and downgrade refusal once
financial evidence exists. The manual-expense, refund, budget, daily, CLI, and
route suites cover the corresponding accounting behavior.

The completed gate for this follow-up is 1,330 passing tests at 89.93%
coverage, with `ResourceWarning` promoted to an error and no warning emitted.
The migration was also rehearsed over a production backup in both directions
before the formal database moved from 0014 to 0015.

## Budget Milestone B9 import tests

Revision `0016_import_batches` is covered by
`tests/migrations/test_import_migration.py` and
`tests/integration/test_cutover_import.py`. The tests prove that preview is
read-only, hash approval rejects changed content, a mid-import constraint
failure rolls every financial row back, replay does not duplicate money,
signed wallet splits preserve negative envelopes, legacy daily activity stays
outside posted totals, immutable triggers reject changes, and downgrade refuses
to erase evidence. Fixtures are synthetic; the real normalized snapshot stays
under the gitignored `data/` tree.

`tests/integration/test_cutover_event_protection.py` adds the post-import
boundary regressions: an event at the exact cutoff is hidden and rejected, a
strictly later event remains actionable, and the shared journal service rejects
an event writer that tries to backdate only its entry. The web route suite also
proves that posting a hidden historical event by URL returns 422 and writes no
journal entry.

Revision `0017_card_web_setup` is covered by
`tests/migrations/test_card_web_setup_migration.py` and the end-to-end card Web
test. It adds only explicit mappings from immutable import evidence to cards,
uses `RESTRICT` foreign keys, refuses a downgrade that would erase a decision,
and proves that assigning a transfer hint creates no scheduled transfer.

For a formal cutover: checkpoint WAL, make a mode-0600 `VACUUM INTO` backup,
upgrade and apply on a copy, verify `integrity_check`, `foreign_key_check`, row
counts, account balances, wallet sums, and the import hash, then upgrade/apply
the formal database. Never use an unspecified Alembic target and never copy a
live WAL database as a lone `.sqlite3` file.

Four properties are easy to get subtly wrong, and each has a test that fails if
the code stops holding them:

- **A day is a local idea.** `test_a_local_day_starts_at_local_midnight` uses
  explicit UTC instants either side of Taipei midnight rather than deriving
  them, so a timezone mistake cannot make the test agree with itself.
- **Totals come from postings, not magnitudes.** A refund, a reversal, and a
  two-category split all have a test whose number is only right when the query
  sums expense ledger lines.
- **A reversal has no event of its own.** The daily row still has to name the
  original event, which is what `reversal_of_id` is followed for.
- **A retry never duplicates money.** Every command has a replay test and a
  conflict test, and matching has one of each for a matched, a pending, and a
  rejected transaction.
- **A note is not a posting.** Note tests snapshot the journal entry and
  allocation before add/change/clear operations, then prove both are unchanged.
  Another test reruns source reconciliation and proves parser-owned refreshes
  preserve the canonical user note.

The B6 note about the bytecode cache applies unchanged to any mutation run
here: purge `__pycache__` and set `PYTHONDONTWRITEBYTECODE=1` around each
mutation, or a caught mutation can report itself as survived. A mutation whose
target text is not found must be reported as skipped, never as caught — a
change that was never applied proves nothing. Two B7 mutations were reported
skipped on their first run for exactly that reason: the anchor text had become
ambiguous after a refactor, and the honest answer was "not applied", not
"caught".

**Idempotent replay is where B7's tests were thinnest, and it is worth knowing
why.** A replay test that only resubmits the *same* request proves the happy
path and nothing else. The interesting failures all keep the token, the amount,
and the description identical and change where the money is: another wallet,
another category, a transfer with its two ends swapped, a second observation
that agrees on every visible field. Each of those looked like a successful
replay while the ledger held something different. Every B7 command therefore
has three tests, not one — an identical resubmission replays, a
different-position resubmission conflicts, and the stored rows are asserted to
be unchanged after the refusal — and each has a mutation that removes the
comparison.

Allocated movements need two independent comparisons. The allocation rows
prove the requested wallet/category split; the journal key and full posting
signature prove that the ledger actually recorded that split. Tests mutate
each layer independently for bank-paid expenses and refunds, including a later
partial refund. They also deactivate a wallet and account after a successful
write and confirm an identical retry still replays: active state governs new
writes, while historical identities remain readable under RESTRICT foreign
keys. Reconciliation account tests rename the Unallocated wallet to ensure the
read model follows `WalletPurpose.UNALLOCATED`, never its display text.

B7 fixtures are invented: `test_bank`, `wallet`, `皮夾`, `測試卡`, last four
`4321`, and invented amounts. Never commit a real bank, account, wallet, card,
merchant, balance, or note.

Scratch B7 setup for local development:

```bash
uv run python -m app money expense-add --merchant "早餐店" \
  --amount-minor 12000 --currency TWD --funding-wallet-id 2 \
  --budget-category-id 1 --client-token scratch-1 \
  --occurred-at 2026-08-09T08:15
uv run python -m app money daily --day 2026-08-09
uv run python -m app money snapshot-add --account-id 1 \
  --observed-balance-minor 1050000 --observed-at 2026-08-09T21:00 \
  --client-token scratch-snap
uv run python -m app money reconcile-show
```

Money-changing commands always take explicit `--amount-minor` and
`--client-token`; only the web forms accept a display amount, and only through
the pure `parse_display_amount`.

## Migration protocol

Since revision `0007` every online migration follows ADR-B027, because SQLite
cannot alter a table constraint in place and `journal_entries` cannot be
rebuilt while foreign keys are enforced. `env.py` disables foreign keys outside
the transaction, runs the migration under `BEGIN IMMEDIATE`, and runs a
complete `PRAGMA foreign_key_check` before committing.

Two details are easy to get wrong and are covered by
`tests/migrations/test_migration_protocol.py`:

- Alembic's SQLite implementation defaults `transactional_ddl` to `False`,
  which turns `begin_transaction()` into a no-op. `env.py` therefore sets
  `transactional_ddl=True` and `transaction_per_migration=False`; without that
  override the pre-commit check cannot roll anything back.
- Foreign keys are unenforced while a migration runs, so the pre-commit check
  is mandatory. Tests inject a failure or an orphan row through
  `config.attributes["verify_before_commit"]`.

Offline `--sql` migrations are refused: they cannot execute or verify the
pragmas the protocol depends on.

A revision that has been applied anywhere is never edited afterwards. Alembic
records only the revision id, so a database that already ran it sees itself as
current and never runs the corrected code; the fix has to arrive as the next
revision, written to run from every shape the released one could leave behind.
Revisions `0009` and `0010` are the worked example.

There is one narrow exception: a faulty data backfill may itself reject a
valid database at the preceding revision, so that database can never reach the
repair revision. In that case the released revision's schema and identity stay
unchanged, its data transform is corrected for databases that have not run it,
and the next revision still re-derives the result for databases already
stamped by the faulty transform. Both paths, plus downgrade/re-upgrade, require
separate regression tests. Revisions `0021` and `0022` are the worked example;
this exception does not permit rewriting an already-published schema shape.

Adding a journal entry type no longer requires touching a financial table.
Insert a row into `journal_entry_types` and extend `JournalEntryType`.

## Migrations

Alembic owns the schema. `python -m app init` applies migrations
programmatically to the configured database.

**The Alembic command line never picks a database on its own.** `alembic.ini`
deliberately carries no `sqlalchemy.url`, so a bare `alembic upgrade head`
fails with `no migration target was named` instead of quietly migrating
whatever database this checkout is configured for. That is not hypothetical:
it is how a real database was upgraded and downgraded by accident during B3B-2
rehearsal, and `SCRIBE_DATABASE_PATH` had no effect because the hard-coded URL
won. Name the target every time:

```bash
SCRIBE_DATABASE_PATH=/tmp/scratch.sqlite3 uv run alembic upgrade head
uv run alembic -x database=/tmp/scratch.sqlite3 current
uv run alembic -x database=/tmp/scratch.sqlite3 heads
uv run alembic -x database=/tmp/scratch.sqlite3 revision \
  --autogenerate -m "describe change"
```

`tests/migrations/test_migration_target.py` proves all three paths: a bare
command refuses, a redirected command migrates only the scratch file, and the
configured database's revision and file checksum are unchanged either way.

Before migrating a database that holds real data: copy it with SQLite's own
`backup()` at mode 600, verify the copy's `integrity_check`,
`foreign_key_check`, revision, and row counts, then rehearse upgrade,
downgrade, and re-upgrade against that copy before touching the original.

A downgrade that would delete financial history refuses instead. Revision
`0011` checks, before any DDL, whether salary rows exist or the journal already
records a `salary_receipt` or `funding_transfer` entry, and aborts if either is
true: dropping those tables would erase a recorded plan, and removing the
registry rows would orphan a posted entry. The refusal reports counts only —
never a template name or an amount — and leaves revision, schema, and data
exactly as they were, so a downgrade is still a safe thing to attempt.

Autogenerated revisions must be reviewed against SQLAlchemy metadata before
commit. Never use `Base.metadata.create_all()` for application initialization.
Every later schema change must test upgrade from the prior revision and document
its rollback or forward-recovery behavior. Repository tests use file-backed
SQLite so WAL and foreign-key behavior match the local application.

## Git workflow

Each milestone ends with:

1. passing quality checks;
2. implementation review;
3. documentation update;
4. one recoverable Git commit;
5. a clean working tree.

## Consolidated dashboard checks

B8 keeps local and external roots deliberately different. Route tests must
assert that local `/` and `/money` render ledger positions while an external
Host sees only the existing parsed-transaction dashboard; `/money` and `/more`
must stay 404 externally. Currency aggregation has pure tests proving card
liability never reduces the asset amount labelled available and TWD/JPY remain
separate.

Responsive tests verify the five primary mobile links, the 760px breakpoint,
44px-or-larger controls, and labelled budget table cells. Browser review uses
360px, 390px, 736px, and desktop widths and checks
`document.documentElement.scrollWidth == document.documentElement.clientWidth`.
The Web application lifespan owns `database.engine.dispose`; any new
composition path must retain that close callback so SQLite connections are not
left for garbage collection.

B8's final gate was run from the committed-shape worktree: Ruff and formatting
passed, mypy checked 214 source files, and pytest completed 1,305 tests at
89.93% coverage. Running the same complete suite with
`-W error::ResourceWarning` also completed with zero warnings. The remaining
logging-test file handle was fixed by closing its handlers in `finally`; this
is test cleanup rather than a change to application log retention.

The browser check used a synthetic revision-0014 database at 360, 390, 736,
768, 900, 1024, and 1440px. Local `/`, `/more`, `/budget/2026-08`,
`/money/accounts`, and `/money/daily` all kept document `scrollWidth` equal to
`clientWidth`, emitted no console warnings or errors, and the new overview's
mobile controls had at least a 44px target. The temporary server and browser
viewport override were both closed after verification.

The clearing page needs stronger evidence than "the table is inside a
scrollable wrapper." Its responsive regression test requires every rendered
table to carry the labelled-card class and every clearing wrapper to opt out of
horizontal scrolling at the mobile breakpoint. Browser QA uses a populated,
synthetic schedule and checks 360, 390, and 736px: the page and every wrapper
must have equal scroll/client widths, the table header must be hidden, each row
must use the card grid, and the route must emit no browser console issue.

The 2026-08-23 phone audit extended that contract to all 30 independent local
GET pages. Each page is checked at 360x800 and 390x844 for document overflow,
table overflow, a visible mobile primary navigation, 40px-or-larger buttons,
and 40px-or-larger checkbox label targets. Email and sync history use labelled
cards at the phone breakpoint; transaction selection keeps a 44px label target;
screen-reader-only reorder labels must be clipped without extending the page.
The audit also clicks the mobile navigation and fills, but does not submit, the
manual-expense form so the evidence covers interaction as well as layout.
