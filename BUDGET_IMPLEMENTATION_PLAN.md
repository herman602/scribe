# Scribe Budget Implementation Plan

Status: **B1, B2, B3A, B3B-1, B3B-2, B4, and B5 accepted; B6, B7, B8, and B9
implemented with their review follow-ups applied; B10 and B11 implemented and
awaiting review; B12, B12A, B13, B14, B15, B16, B17, B18, B19, B20 implemented
and awaiting review; B20A, B20B, B20C, and B20D applied after review**

Architecture authority: [`BUDGET_ARCHITECTURE.md`](BUDGET_ARCHITECTURE.md)

This is the execution contract for an implementation agent. It converts the
accepted budget architecture and Prototype feedback into small, reviewable
milestones. It does not authorize implementing more than one milestone at a
time.

## 1. Baseline and handoff rules

The plan was written against this repository state:

- Functional-code baseline: `a057ecf` (`docs: record budget architecture
  decisions`); implementation starts from the later documentation-only plan
  commit at repository HEAD.
- Alembic head: `0004`
- Existing parsed `transactions.email_id` is mandatory.
- Existing `transactions` are parser/source observations, not ledger rows.
- Existing web writes use `_validated_form`, CSRF cookies, optimistic checks,
  and an exact origin policy.
- Existing external routes are deliberately narrow. Budget and account routes
  must remain localhost-only unless a later ADR explicitly broadens them.
- Existing `LocalSyncWorker` belongs to Gmail synchronization and must not
  become the general money scheduler.

Before starting any milestone, the implementation agent must:

1. Read this document, `BUDGET_ARCHITECTURE.md`, `ARCHITECTURE.md`,
   `PRIVACY.md`, and current source code.
2. Verify `git status --short` is clean or identify unrelated user changes.
3. Verify the current Alembic head instead of assuming the revision number.
4. State the milestone goal, decisions, and expected file list.
5. Implement only that milestone.
6. Run Ruff, Ruff format check, mypy, pytest, and migration tests.
7. Review security, privacy, money arithmetic, idempotency, and rollback.
8. Update documentation and create one reversible Git commit.
9. Stop for review. Do not begin the next milestone automatically.

No implementation agent may:

- run a new migration against the user's real database before testing it on a
  temporary copy and creating a verified backup;
- copy real balances, account numbers, merchant history, Email bodies, or card
  data into source, fixtures, logs, screenshots, or commits;
- silently reinterpret an accepted ADR;
- drop an existing table or column merely because the new model supersedes it;
- expose `/money`, `/budget`, `/allocations`, `/clearing`, `/subscriptions`, or
  `/cards` through the external hostname;
- add Redis, Celery, Node.js, a cloud database, or an external LLM;
- use `float` for money;
- post a journal entry from an uncertain Gmail transaction;
- silently merge two possible source transactions;
- mutate a posted journal entry instead of reversing it.

## 2. Product behavior to deliver

### Daily operation

1. Gmail continues to synchronize and parse bank messages message by message.
2. Confirmed source transactions appear in a money review queue.
3. The user chooses budget category and funding wallet.
4. Posting creates one immutable financial event and balanced journal entry.
5. The user may add a cash or credit-card expense from the daily page.
6. A later Gmail notification can link to the manual credit-card event without
   posting the expense twice.
7. Interest, fees, refunds, direct debits, and other bank movements can be
   recorded against any bank account and wallet.

### Monthly operation

1. Subscription requirements are calculated from the effective-dated register.
2. A salary allocation run freezes the applicable template and subscription
   amount for the calendar month.
3. Same-bank allocations reclassify wallets when the run is approved.
4. Cross-bank salary allocations remain commitments until the user presses
   **已轉帳**.
5. First Bank `帳單暫存` transfers and card autopay use scheduled assumptions.
6. At month close, unused budget moves to a configured destination wallet;
   intentional living-expense buffer remains separate.
7. Bank-app balance snapshots reveal reconciliation differences without
   overwriting the ledger.

### Main pages

- `/money` — actual, reserved, available, and reconciliation overview.
- `/money/daily` — date-first activity with all/cash/credit-card filters and
  one add-expense action.
- `/money/accounts` and `/money/accounts/{id}` — accounts, wallets, ledger,
  external deposits/debits, and snapshots.
- `/money/review` — confirmed source events waiting for allocation or source
  matching review.
- `/budget/{yyyy-mm}` — target, actual, remaining, intentional buffer, and
  month close.
- `/allocations/{yyyy-mm}` — salary run and manual transfer confirmations.
- `/clearing` — First Bank clearing commitments and scheduled transfers.
- `/subscriptions` — canonical subscription register and monthly requirement.
- `/cards` — issuer, last four, holder role, liability, settlement account, and
  default funding wallet.
- `/money/reconcile` — observed balances and ledger differences.

The existing `/`, `/transactions`, `/emails`, `/reports`, and `/sync` pages
remain functional throughout the work.

## 3. Cross-cutting domain rules

### Money and currency

- Store `amount_minor` as integer and currency as uppercase ISO code.
- Event amounts are non-negative. Direction comes from event/posting type.
- Journal posting amounts are signed integers and balance to zero per currency.
- Resolve display exponent through the existing currency-aware money formatter.
- Never aggregate TWD, JPY, USD, or any other currencies into one amount.

### Ledger and wallets

- A physical asset account has one corresponding asset `ledger_account`.
- Every posting to a wallet-managed asset account has `wallet_id`.
- Missing wallet selection resolves explicitly to that account's `Unallocated`
  wallet; it is never `NULL` by accident.
- Wallet sum equals the calculated balance of its parent asset account.
- No balance column is incremented directly; balances are journal queries.
- Posted entries are immutable. Correction creates reversal and replacement.
- A schedule reserves availability but changes actual balance only when posted.

### Time

- Store instants in UTC using the existing `UTCDateTime` type.
- Convert calendar dates with injected `ZoneInfo(settings.local_timezone)`.
- The accepted local timezone is `Asia/Taipei` by default.
- Schedule `effective_at` is the due instant. `processed_at` records catch-up.
- The provisional 2026-08-07 cutover date was superseded before any opening
  entries were posted. The accepted cutover observation is 2026-08-14
  23:10:28 Asia/Taipei; each opening snapshot retains that true observation
  time.

### Idempotency and concurrency

- Every form that can create money accepts a random client idempotency token.
- Every journal-producing use case derives a stable unique idempotency key.
- User edits use integer `version` or exact `updated_at` optimistic checks.
- Worker claims use one atomic conditional update and an expiring lease.
- Retrying POST, CLI, sync intake, import, or worker catch-up cannot double-post.

### Source observations

- Gmail thread ID is navigational metadata only. Intake uses Gmail message ID
  and existing parsed transaction identity.
- Parsed values remain parser-owned; effective manual values remain protected.
- Only confirmed, non-ignored transactions enter money intake.
- Source changes after posting create a discrepancy for review.
- Candidate source links do not create or reverse journal entries.

### Privacy

- Store institution name/code, display name, and optional masked reference only.
- Card identity uses institution plus last four; never full PAN.
- Do not log money amounts, notes, balance values, account references, merchant
  evidence, or source Email content in new services.
- Import and export stay local. Do not call Google Sheets API or any LLM.

## 4. Target dependency and package layout

Use the current modular monolith. Add modules only when the milestone needs
them; do not pre-create empty abstractions.

```text
app/
├── domain/
│   ├── budget_enums.py
│   ├── budget_models.py
│   ├── budget_rules.py
│   └── ports/
│       └── budget_repositories.py
├── application/
│   ├── dto/
│   │   └── budget.py
│   ├── queries/
│   │   ├── money.py
│   │   ├── budget.py
│   │   └── clearing.py
│   └── services/
│       ├── ledger.py
│       ├── financial_event_intake.py
│       ├── event_allocation.py
│       ├── subscriptions.py
│       ├── salary_allocation.py
│       ├── scheduled_transfers.py
│       ├── manual_expenses.py
│       └── reconciliation.py
├── infrastructure/database/
│   ├── budget_models.py
│   ├── budget_repositories.py
│   ├── budget_queries.py
│   └── migrations/versions/
├── presentation/
│   ├── cli/money.py
│   └── web/
│       ├── routes/money.py
│       ├── templates/money/
│       └── static/money.js
└── worker/
    └── local_maintenance.py
```

The existing `app/bootstrap.py` remains the composition root. New repository
ports are exposed by the existing Unit of Work rather than by opening ad-hoc
sessions in services or routes. Read-only aggregate repositories may receive a
session factory, following current Dashboard query conventions.

If `app/presentation/web/app.py` becomes difficult to navigate, extract a money
router in B3 or later. Do not restructure existing routes in B1 just for style.

## 5. Migration sequence

The revision IDs below assume current head `0004`. If another revision exists
when work starts, preserve the milestone order but rebase IDs on the real head.

| Milestone | Expected revision | Tables introduced |
| --- | --- | --- |
| B1 | `0005_ledger_foundation` | `financial_accounts`, `wallets`, `ledger_accounts`, `journal_entries`, `journal_postings` |
| B2 | `0006_cards_and_financial_events` | `cards`, `financial_events`, `financial_event_source_links` |
| B3 | `0007_budget_allocations` | `budget_categories`, `budget_periods`, `budget_targets`, `event_allocations`, `funding_commitments` |
| B4 | `0008_subscriptions` | `subscriptions`, `subscription_price_periods` |
| B5 | `0011_salary_allocations`, `0012_salary_regeneration_provenance` | salary template/run tables |
| B6 | `0013_scheduled_transfers` | `scheduled_transfers`, `scheduled_transfer_commitments` |
| B7 | `0014_reconciliation` | `balance_snapshots` and only matching-support columns proven necessary |
| B8 | none expected | UI/query integration only |
| Dedicated-wallet follow-up | `0015_dedicated_wallet_spending` | `dedicated_wallet_allocations` |
| B9 | `0016_import_batches` | `import_batches`, `import_rows`, cutover metadata if needed |
| Card Web setup | `0017_card_web_setup` | reviewed card-cycle and transfer-hint assignments |

Migration requirements:

- Every enum-like string has a check constraint.
- Every foreign key declares intended delete behavior; financial history uses
  `RESTRICT`, never cascade deletion.
- Add all indexes named in architecture plus composite indexes for due jobs,
  daily activity, source matching, and reconciliation.
- Alembic upgrade must preserve all existing application tables and rows.
- Downgrade is tested on a temporary database. Never downgrade the user's real
  database as an ordinary rollback strategy.
- SQLite table rebuild migrations must explicitly preserve indexes, foreign
  keys, UTC values, and row counts.

## 6. Global quality gate

Run from repository root after every milestone:

```bash
git diff --check
UV_CACHE_DIR=/tmp/scribe-uv-cache uv run ruff check app tests
UV_CACHE_DIR=/tmp/scribe-uv-cache uv run ruff format --check app tests
UV_CACHE_DIR=/tmp/scribe-uv-cache uv run mypy app tests
UV_CACHE_DIR=/tmp/scribe-uv-cache uv run pytest -q
```

Also run milestone-specific migration tests and ensure total coverage remains at
least the configured 85%. Passing only a selected test file is not sufficient.

## 7. Budget Milestone B1 — Ledger foundation

Status: **accepted at revision `0005_ledger_foundation` (`eb24f51`).**
Decisions taken while implementing this milestone:

- Signed postings use debit-positive/credit-negative integers, so an asset
  account's calculated balance is positive and a card liability's is negative.
- A physical ledger account code is `{kind}:{institution_code}:{account_id}`,
  and opening equity is one `equity:opening:{currency}` account per currency.
- Opening positions use the stable key
  `opening_balance:financial_account:{id}` and reversals use
  `reversal:journal_entry:{id}`, so retrying either is idempotent.
- A matching idempotency key is never treated as proof that two requests are the
  same work. When a key already exists, the service compares entry type,
  reversal target, effective instant, and the order-independent posting
  signature; a different payload raises `IdempotencyConflictError` instead of
  returning an unrelated entry. The description is deliberately excluded because
  it is display text and does not change the accounting effect.
- Only `JournalService.reverse` can create a reversal. `post_balanced` has no
  `reversal_of_id` parameter, an entry typed `reversal` must name its target, a
  non-reversal must not name one, and a reversal's postings must be the exact
  opposite of the reversed entry. This removes the "fake reversal" path that
  could consume the unique `reversal_of_id` slot without cancelling anything.
- The paired `entry_type`/`reversal_of_id` rule is enforced in the application
  rather than as a SQLite check constraint. Adding it to `journal_entries` would
  require a batch table rebuild, and on SQLite that rewrites the dependent
  `journal_postings` foreign key through the temporary table. A reviewer who
  wants the database-level guarantee should schedule it with B2's revision so
  the rebuild can be planned and tested on its own.
- `journal_entries.entry_type` is currently limited to `opening_balance`,
  `internal_transfer`, `adjustment`, and `reversal`. B2 and later milestones
  extend the enum together with its check constraint.
- "Wallet belongs to the posting's physical financial account" and per-currency
  balancing are application invariants with tests, because SQLite cannot express
  either as a row-level constraint without triggers.
- `financial_accounts.masked_reference` additionally rejects five or more
  consecutive digits in both the domain and the database.

### Goal

Create a safe accounting kernel for accounts, wallets, opening balances,
balanced journal entries, reversals, and balance queries. No Gmail integration,
budget UI, salary flow, or scheduling belongs in B1.

### Schema

Implement revision `0005` with:

- `financial_accounts`: institution code, display name, type, currency, masked
  reference, active flag, optimistic version, timestamps.
- `wallets`: parent account, name, purpose, active flag, version, timestamps.
- `ledger_accounts`: code, name, kind, currency, optional unique physical
  financial-account link.
- `journal_entries`: type, effective/recorded timestamps, unique idempotency
  key, optional reversal link, description, timestamps.
- `journal_postings`: entry, ledger account, optional wallet, signed amount,
  currency, timestamps. Budget-category linkage is added only when the category
  table exists in B3.

Constraints:

- unique wallet name per financial account;
- one active `unallocated` wallet per physical asset account;
- one physical ledger account per financial account;
- nonzero posting amount;
- reversal points to another entry, cannot reverse itself, and is unique so the
  original cannot be reversed twice;
- wallet belongs to the posting's physical financial account;
- application validation balances each currency before commit.

### Domain and services

- Add typed `Money`, account, wallet, journal draft, posting draft, and balance
  DTOs without SQLAlchemy imports.
- Add `JournalService.post_balanced`, `reverse`, and `record_opening_position`.
- `record_opening_position` requires a split whose total equals the supplied
  account balance; unresolved money goes through an explicit `Unallocated`
  line rather than hidden arithmetic.
- Opening wallet shares are signed. A wallet that was already overdrawn at the
  observation time remains negative, while a zero share creates no posting;
  the complete signed split must still equal the positive physical account
  balance exactly.
- Opening positions balance against a system-created opening-equity ledger per
  currency. Asset openings debit the asset; liability openings credit the
  liability. The service never inserts a one-sided balance.
- Add account/wallet repository ports and implementations.
- Extend the Unit of Work protocol and SQLAlchemy implementation.
- Add a read query returning account balance and wallet totals from postings.

### Minimal vertical interface

Add a `money` Typer sub-application with diagnostic/setup commands sufficient
for tests and developer use:

```text
python -m app money account-add
python -m app money wallet-add
python -m app money opening-balance
python -m app money balances
```

Prompts/arguments must never echo sensitive full account numbers. CLI business
logic delegates to application services.

### Expected files

- New budget domain/enums/ports and `application/services/ledger.py`.
- New database models/repositories/query module and revision `0005`.
- Unit of Work and bootstrap wiring.
- `presentation/cli/money.py` and small registration change in current CLI.
- Unit, repository, migration, and CLI tests.
- README/DEVELOPMENT update for scratch ledger setup.

### Acceptance tests

- Two-currency journal draft is rejected unless each currency balances.
- Wallet sum equals physical account balance after opening and internal move.
- Retrying the same idempotency key returns the same entry.
- Reversal creates opposite postings exactly once.
- Missing/mismatched wallet, account, or currency rolls back everything.
- Existing Gmail tables retain row counts across upgrade.
- Foreign keys remain enabled and migration head is reported correctly.

### Commit

Suggested message: `feat: add immutable ledger foundation`

Stop after B1 review.

## 8. Budget Milestone B2 — Cards and source-event bridge

### Goal

Introduce canonical financial events, rich local card configuration, and an
auditable bridge from confirmed parsed transactions without posting expenses
yet.

### Schema

Implement revision `0006`:

- `cards`: Gmail account, issuer, display name, last four, holder role,
  configuration status, nullable liability/settlement references while setup is
  incomplete, optional default funding wallet, active range, version,
  timestamps. Application rules require liability and settlement before status
  becomes `ready` or a card expense can post.
- `financial_events`: origin, payment method, recognition basis, type, occurred
  time, merchant, amount/currency, optional card, status, note, version,
  idempotency key, timestamps.
- `financial_event_source_links`: event, existing parsed transaction, candidate/
  confirmed/rejected status, integer score basis points, bounded reasons,
  reviewer time, timestamps.

Do not drop `supplementary_card_settings`. Backfill cards from it and migrate
issuer/last-four/holder role only; never guess settlement or funding accounts.
Migrate reads/writes deliberately and keep legacy data until B9 proves it
unnecessary.

Revision `0006` also adds nullable `financial_event_id` to `journal_entries`,
because that referenced table does not exist during B1.

### Source bridge

- Intake only manually confirmed, non-ignored effective transactions.
- Map issuer plus last four to a local card. Missing or ambiguous card leaves
  the event in a visible setup/review state.
- New Gmail-origin event status is `needs_allocation`.
- Intake is idempotent by parsed transaction ID.
- Bulk and detail confirmation invoke intake in the same Unit of Work and
  database transaction; do not add a second outbox for this local operation and
  do not allow a silent half-state.
- Existing confirmed transactions can be staged with an explicit backfill CLI.
- Parser rerun never overwrites a posted event. Record a discrepancy if effective
  source fields no longer agree.

### Minimal vertical interface

- `/cards` list/edit page, local-only.
- CLI `python -m app money intake-confirmed --dry-run` and explicit apply mode.
- `/money/review` can initially be a simple event list with reason/status; full
  allocation arrives in B3.

### Matching preparation

Implement the source-link repository and pure match scorer. B2 has no manual
credit-card **expense** UI: `/cards` may create and edit card *configuration*,
but nothing in B2 may create a manual expense or any other financial event
outside Gmail intake. Match scorer inputs are:

- exact currency and amount (mandatory for any automatic link);
- compatible card identity when known;
- transaction type compatibility;
- bounded occurred-at window;
- normalized merchant evidence as supporting, never sole, evidence.

Store reasons without Email body excerpts or full merchant evidence.

### Acceptance tests

- One parsed transaction creates at most one confirmed source link.
- Unconfirmed, ignored, or deleted-source transactions are not staged.
- Missing card mapping is safe and user-readable.
- Existing supplementary settings backfill to the correct bank/last-four role.
- Reparse changes create discrepancy and do not mutate journal history.
- Thread IDs never participate in event uniqueness.

### Commit

Suggested message: `feat: add cards and financial event intake`

Stop after B2 review.

### B2 implementation status and decisions

Status: **accepted at revision `0006_cards_and_financial_events` (`32a13ae`).**
Decisions taken while implementing this milestone:

- `financial_events.amount_minor` is a positive magnitude. The domain rejects
  `float`, `bool`, zero, and negative values, and the database adds
  `CHECK (amount_minor > 0)`. Direction comes from `event_type` alone.
- Every card-mapping and parser-rerun outcome is persisted as a stable code
  (`SourceLinkReason`, `SourceDiscrepancy`, `IntakeSkipReason`) inside
  `reason_json`, never as display text and never with merchant evidence,
  amounts, or Email content.
- A Gmail-origin event is always created with status `needs_allocation`. An
  unresolved card is expressed by `card_id IS NULL` plus a reason code, so the
  status rule in this plan stays literally true.
- Intake refuses to guess. A transaction whose effective type is `unknown`, or
  which lacks an amount, currency, or occurred-at value, is skipped with a
  stable reason instead of being forced into an `adjustment` event; ADR-B023
  reserves `adjustment` for genuine reconciliation.
- A parser rerun refreshes only `draft`, `needs_allocation`, and `ready`
  events. A `posted`, `reversed`, or `ignored` event is never modified; the
  rerun records discrepancy codes on the confirmed source link instead.
- Direct Gmail intake creates the only `confirmed` link. The match scorer can
  produce candidates only: `candidate_link_draft` hard-codes
  `SourceLinkStatus.CANDIDATE`, so a score can never promote itself.
- `journal_entries` is extended in place rather than rebuilt. A batch rebuild
  must `DROP TABLE journal_entries`, whose implicit row-by-row delete violates
  the table's own `reversal_of_id` ON DELETE RESTRICT foreign key once a
  reversal exists. `PRAGMA defer_foreign_keys` does not help, because RESTRICT
  is always checked immediately, and `PRAGMA foreign_keys` cannot be changed
  inside a migration transaction. Both behaviours were verified empirically
  before choosing the approach.
- Consequently the paired reversal invariant is enforced by BEFORE INSERT and
  BEFORE UPDATE triggers instead of a CHECK constraint, and
  `journal_entries.financial_event_id` is added with an inline `REFERENCES ...
  ON DELETE RESTRICT` clause. SQLite enforces both; a migration test asserts
  them through `PRAGMA`. Because SQLAlchemy cannot reflect a column-level
  constraint's name or ON DELETE, `env.py` excludes exactly that one foreign
  key from autogenerate comparison.
- Card readiness is derived, never set by a form. Ready requires a
  `card_liability` liability account and an asset settlement account, an
  optional funding wallet belonging to an active asset account, all sharing one
  currency, plus a four-digit last four.
- `supplementary_card_settings` is not dropped. Backfill migrates only issuer,
  last four, and the supplementary role; it is idempotent and never overwrites
  a card the user has since edited.

Applied after the first B2 review:

- A matching intake key is never treated as proof of ownership. On a hit the
  service checks origin, recognition basis, and that a confirmed link points at
  exactly this event and transaction; a mismatch raises
  `IdempotencyConflictError` so the whole confirming transaction rolls back.
  A legitimate retry runs the same reconciliation as a parser rerun instead of
  skipping, so an edited amount or a card configured later is picked up.
- Every transaction review reconciles the bridge, not only a confirmation.
  Unconfirming or ignoring an already bridged transaction records
  `source_no_longer_confirmed`; only a first confirmation also stages.
- `FullGmailSync` receives the intake service, so full syncs and the
  post-history-expiry reconciliation fallback reconcile bridged events exactly
  like incremental sync and manual reparse.
- Source-link `reasons` describe the mapping as it stands now and are replaced
  on reconciliation, so configuring a card later clears `card_not_configured`.
  `discrepancies` remain the accumulating audit trail.
- Revision `0006` refuses to upgrade, before creating anything, when any
  pre-existing `journal_entries` row already violates the reversal pairing
  invariant, because a trigger cannot validate rows that already exist.

Applied after the second B2 review:

- `backfill` apply mode always commits. Staging is not the only write:
  reconciling an already staged event can update the event and its mapping
  reasons, and committing only when something new was staged discarded exactly
  that work, so a card configured later never persisted. `IntakeResult` now
  also reports `reconciled`, and the CLI prints it, so an apply run that
  changes only existing events is visible rather than looking like a no-op.
- `reconciled` counts the already staged events an apply run passed through
  reconciliation. It does not promise that each one changed a field, and a dry
  run always reports zero because it reconciles nothing. A precise preview
  would need to split the count into would-reconcile and updated; that is a
  deliberate later refinement, not a defect.

## 9. Budget Milestone B3 — Budget periods, allocation, and posting

### Goal

Turn reviewed financial events into categorized, funded, immutable ledger
entries and expose a calendar-month budget vertical slice.

### Schema

Implement revision `0007`:

- hierarchical `budget_categories` with one currency-neutral identity;
- `budget_periods` unique by year/month/currency;
- `budget_targets` with target, intentional buffer, and optional month-end
  destination wallet;
- `event_allocations` with category, funding wallet, amount/currency, and source;
- `funding_commitments` with source wallet, settlement destination, amount,
  currency, status, and event/allocation identity.
- nullable category linkage on nominal ledger accounts/postings, with one
  expense ledger identity per category/currency where required.

Allocation sum must equal event amount before posting. A category answers what
the expense is; a wallet answers which money funds it.

### Posting behavior

- Credit card purchase: debit expense/category; credit card liability.
- Cash purchase: debit expense/category; credit cash asset wallet.
- Direct bank debit: debit expense/category; credit bank asset wallet.
- Refund: reduce the appropriate liability or increase asset and credit the
  original expense/refund category exactly once.
- Card purchase reserves its funding wallet. If funding is cross-bank, create a
  transfer commitment; do not pretend the destination bank already has funds.
- Mark financial event `posted` only after event, allocations, commitments, and
  balanced journal commit atomically.

### Application/UI

- `AllocateFinancialEvent` with optimistic version and client idempotency token.
- `/money/review` allocation form with split support.
- `/budget/{yyyy-mm}` target/actual/remaining/buffer values by currency.
- `CloseBudgetPeriod` preview and commit. It transfers only unused target amount
  to the configured wallet; buffer stays unless overspending consumed it.
- Overspend is explicit and does not create negative phantom budget.

### Queries

Keep aggregate SQL in `budget_queries.py`. Routes receive DTOs. Required values:

- period target, net spend, remaining, overspend;
- intentional buffer target/remaining;
- wallet ledger balance, reserved, and available;
- events needing allocation;
- open funding commitments.

### Acceptance tests

- Purchase posts expense exactly once.
- Split allocation must total exactly and use one currency.
- Card expense changes liability but does not reduce a bank balance at purchase.
- Cash/direct debit reduces only the selected asset wallet/account.
- Budget totals include refunds with correct direction.
- Month close is previewable, idempotent, and cannot sweep more than available.
- Buffer is displayed separately and is not part of monthly spendable target.

### Commit

Suggested message: `feat: add budget allocation and posting`

Stop after B3 review.

### B3 is delivered as two reviewable commits

A milestone may land as more than one cohesive, revertible commit. B3 is split
because its migration protocol is independently reviewable and carries the
milestone's real risk.

**B3A — migration and schema foundation.** Revision `0007`, the ADR-B027
migration protocol in `env.py`, the `journal_entry_types` registry, the B3
tables and columns, the domain enums and rules, and their tests. It adds no
repository, service, query, route, or CLI command, so nothing yet reads or
writes the new tables. The real database is deliberately left at `0006` until
B3B makes the schema useful.

Status: **accepted at `10d61b9`.** Notes:

- Alembic's SQLite implementation defaults `transactional_ddl` to `False`,
  which makes `begin_transaction()` a no-op context manager. The pre-commit
  `PRAGMA foreign_key_check` would then run after the schema was already
  committed and could not roll anything back, so `env.py` sets
  `transactional_ddl=True` and `transaction_per_migration=False` explicitly.
  Two tests prove it: a failure injected after the DDL rolls back schema, data,
  and revision together, and an orphan created while enforcement is off blocks
  the commit. Both fail if the override is removed.
- Migration tests inject those failures through
  `config.attributes["verify_before_commit"]`, Alembic's supported extension
  point, which `env.py` calls at the last moment the transaction can still roll
  back.
- `CommitmentStatus` includes `partially_scheduled` from the start. The status
  set already anticipates scheduling, and omitting one value would force
  another migration of a financial table in B6.
- Revision `0006`'s downgrade now rebuilds `journal_entries` instead of using
  `ALTER TABLE ... DROP COLUMN`: after `0007` the event link is a table-level
  foreign key, and SQLite refuses to drop a column named in one.
- `financial_events.refund_of_event_id` carries a row-level shape constraint:
  `refund_of_event_id IS NULL OR (event_type = 'refund' AND
  refund_of_event_id <> id)`. Whether the target is itself a refund is a
  cross-row rule that B3B's service enforces.
- Two SQLite batch behaviours are load-bearing and easy to get wrong:
  `create_check_constraint` inside a `copy_from` batch silently does nothing,
  so the refund column is added with a plain `ALTER` first and the rebuild then
  applies the full definition; and a batch block with no operations does not
  rebuild at all unless `recreate="always"` is passed.
- The round-trip test seeds a real financial event, a confirmed source link,
  and a journal entry whose `financial_event_id` is not null, and compares
  descriptions and every timestamp. It was verified by making `0007` null the
  link and confirming the test fails.

**B3B — allocation, posting, and the budget slice.** Delivered in parts.

*B3B-1 — records, repositories, and journal event linking.* Status:
**accepted at `6f20934`.**

- domain records and drafts for categories, periods, targets, allocations, and
  commitments, plus the ports, SQLAlchemy repositories, and Unit of Work
  wiring;
- `JournalService.stage_balanced` accepts a caller's Unit of Work, so a later
  command can commit an allocation, its commitments, the journal entry, and the
  event's new status as one atomic change. Every journal invariant is still
  validated by `JournalService`; a caller never reaches the journal repository;
- `journal_entries.financial_event_id` is now written, is part of the
  idempotency payload comparison, and is guarded so one event can hold at most
  one primary entry. A reversal continues to use `reversal_of_id` only;
  (since `0020` the guard is "at most one entry still in force", enforced by
  the posting service rather than a unique index, so a redone allocation can
  stage a replacement, and since `0021` each allocation row names the entry it
  was posted with — corrected in `0021` for future upgrades and re-derived by
  `0022` for databases already stamped by the faulty backfill — so the split
  in force can be told from a superseded one — ADR-B103);
- `journal_postings.budget_category_id` is carried through the posting draft;
- `FundingCommitment.release` is bounded by the outstanding amount, so a
  reservation can never be released twice.

Applied after the first B3B-1 review:

- `budget_category_id` is part of journal identity. It joins the posting
  signature, so reusing one idempotency key with a different category is a
  conflict rather than a silent match; `negated_postings` and
  `JournalService.reverse` carry it, so a categorized expense and its reversal
  cancel exactly; and `ensure_exact_reversal` now detects a category mismatch.
- The 0007 columns reach the domain: `LedgerAccountDraft`/`LedgerAccount` carry
  `budget_category_id` with a `get_for_budget_category(category_id, currency)`
  lookup, and `FinancialEventDraft`/`FinancialEvent` carry
  `refund_of_event_id`. `JournalService` also rejects a posting whose category
  disagrees with a category-bound ledger account.
- Every minor-unit input goes through one `validate_minor_units` helper, which
  rejects `float` and `bool` — `bool` is an `int` subclass, so a stray flag
  would otherwise be stored as 0 or 1 money. Targets and buffers must be
  non-negative, commitments and releases positive, and commitment currency is
  normalized at the domain boundary.
- Only `stage_balanced` may link a financial event. The standalone
  `post_balanced` no longer accepts `financial_event_id`, because posting an
  event must commit its allocations, commitments, and status in the same
  transaction.
- Optimistic locking is not optional: creating a target requires no version,
  updating requires the exact current one, and each mismatched combination is a
  conflict. The update is a conditional `UPDATE ... WHERE version = :expected`
  with a row-count check, and `release` is likewise a conditional statement, so
  neither has a read-modify-write race.

Applied after the second B3B-1 review:

- The category check on a posting is equality in both directions. Only
  validating category-bound ledger accounts still allowed a posting to tag an
  equity or asset line with a budget category, which would have polluted
  category totals.
- Revision `0007` adds two shape constraints so the database agrees:
  `budget_category_id IS NULL OR kind = 'expense'`, and a category ledger may
  not also be a physical financial account. Expense ledgers without a category
  stay legal, because B3's schema calls the category linkage nullable; a
  bidirectional pairing would forbid an uncategorized expense line that a later
  milestone may still need.
- `BudgetCategoryRepository.save` is now the same conditional
  `UPDATE ... WHERE version = :expected` used by targets, so an interleaved
  writer cannot be overwritten by a stale form.
- Known remaining instance of the same pattern: `cards.save_configuration`
  still reads and then writes. It was accepted in B2 and is left unchanged here
  rather than widened into this review; it should move to a conditional update
  when card configuration is next touched.

*B3B-2 — allocation, posting, refunds, and the budget slice.* Status:
**implemented; awaiting review.**

- `AllocateFinancialEvent.execute` opens one Unit of Work and commits the
  allocations, the funding commitments, the balanced journal entry, and the
  event's new status together. It reaches the journal only through
  `stage_balanced`, so every journal invariant is still checked in one place.
- A card purchase moves the card liability and reserves funding; it never
  reduces a bank balance, because no money has left the account yet. Anything
  else reduces exactly the wallets the user selected. The commitment's
  `fulfillment_mode` comes from account identity (ADR-B029), never from
  comparing bank display names.
- A refund returns to the category that was originally spent (ADR-B030).
  `propose_refund_lines` inherits the original split and spreads a partial
  refund with largest-remainder distribution, so the parts always sum back
  exactly and ties resolve by row order rather than by however the rows came
  out of the database.
- The cross-row rule the row-level constraint cannot express lives here: a
  refund may not refund another refund, must use the same currency, and may not
  exceed the event it refunds. All three are enforced in `propose_refund_lines`
  and in `execute`, so neither path can post one.
- Allocation is idempotent by `(financial_event_id, client_token)`. A
  resubmitted form replays the first entry; the same token with a *different*
  split is rejected rather than answered with the earlier outcome, and a token
  belonging to another event is refused outright.
- `CloseBudgetPeriod` previews before it writes. The sweep is a
  reclassification between wallets of one physical account, capped at the
  wallet balance minus what open commitments already hold and minus every
  buffer funded from that wallet. A destination in another account is reported
  as needing a real bank transfer and posts nothing. Closing twice replays the
  first entry rather than sweeping again.
- The buffer is reported beside the target and is never part of the month's
  spendable target or of what a close may sweep (ADR-B031).
- Category and target management ship with the milestone: nesting is capped,
  a category cannot become its own ancestor, deactivating never deletes, and a
  target must be funded from a wallet in the period's own currency.
- `/budget/{yyyy-mm}`, the split allocation form on `/money/review`, and the
  `category-add`, `category-list`, `target-set`, `budget-show`, and
  `budget-close` CLI commands. Every budget route is local-only; the external
  hostname receives 404 for all of them, read and write alike.
- A month is a local calendar month resolved in `local_timezone` and compared
  against UTC storage, so a purchase at 23:30 on the 31st in Taipei belongs to
  that month and not the next one.
- `default_currency` is a new setting rather than a hard-coded `"TWD"`, so the
  budget page and CLI have one place to read the period currency from.

Applied after the first B3B-2 review:

- Only spending event types may be allocated: `purchase`, `fee`,
  `subscription`, `bill_payment`, and `direct_debit`, plus `refund`. A
  `deposit`, `withdrawal`, `transfer`, `interest_income`, or `adjustment` was
  previously posted as an expense and reduced a wallet, inventing spending that
  never happened. Each of those needs its own posting rule and is refused with
  `UnsupportedEventTypeError` until it has one.
- Refunds are now capped cumulatively and per share. Checking one refund
  against the original let two partial refunds each pass and together
  over-credit the category and the card liability. The service reads every
  posted refund of the original inside the same transaction, caps the total,
  and additionally caps each `(wallet, category)` share, so a refund cannot
  credit one category for another category's spending.
- A refund must name an original that is already `posted` and has allocations,
  and must return to the same card. Refunding onto a different instrument would
  credit one card while releasing another card's reservation; that is a
  transfer plus a refund and needs its own model rather than a silent guess.
- `propose_refund_lines` now spreads across what each share can *still* return,
  so a second partial refund proposes the remainder instead of the original
  split.
- A funding wallet must hold the event's currency. The form filtered the list,
  but a tampered POST or a direct service call could reserve a TWD amount out
  of a USD wallet and reduce that wallet's available balance by a foreign
  minor amount.
- A buffer already consumed by overspending is no longer withheld twice at
  month end. `remaining_buffer_minor` subtracts the overspend, so a sibling
  category funded from the same wallet can still sweep what the wallet
  actually holds. The page and the CLI now show the buffer target and the
  remaining buffer side by side.
- Budget totals no longer omit spending. The month view emits a row for
  spending in a category with no target for that month, and a row for
  uncategorized spending, and reports both even when no budget period exists
  yet. Net spend is read from expense ledger lines only, so an asset or
  liability leg is never counted as spending.
- The month-end sweep is posted as `budget_close`, not `internal_transfer`, so
  an audit can tell a month end apart from an ordinary wallet transfer.
- A target can now be corrected. The page carries the target's id and version
  and renders an edit form per planned category; `money target-set` refuses a
  blind overwrite and names the version to pass as `--expected-version`. The
  page also renders real category rename, re-parent, and deactivate forms.
- `alembic.ini` no longer hard-codes `sqlalchemy.url`. A bare
  `alembic upgrade head` refuses instead of migrating the configured database,
  which is how a real database was upgraded and downgraded by accident during
  rehearsal. The target must come from `SCRIBE_DATABASE_PATH`, from
  `-x database=<path>`, or from the programmatic config, and a regression test
  asserts the configured database's revision and file checksum are unchanged.

Applied after the second B3B-2 review:

- A refund must use the payment method of the event it refunds, not only the
  same card. A card purchase refunded as a `bank_account` credit raised a bank
  balance, left the card liability where it was, and still released the card's
  reservation, so the two halves of the same refund disagreed permanently.
  Both `propose_refund_lines` and `execute` refuse it, and the whole
  transaction rolls back: no entry, no allocation, no release, no balance
  movement.
- A refund releases exactly the shares it returned money to. The release was
  spread across every outstanding commitment in proportion to its balance, so
  refunding only the food share of a food 70,000 / transport 30,000 purchase
  released food 49,000 and transport 21,000 instead of food 70,000. When the
  two shares are funded from different wallets that misstates both wallets'
  reserved and available balances. Each refunded `(wallet, category)` share is
  now matched back through `event_allocation_id` to the commitments created for
  the original allocations of that same share.

## 10. Budget Milestone B4 — Subscription register

### Goal

Replace manually copied subscription totals with one effective-dated register
and a deterministic monthly funding requirement.

### Schema and rules

Implement revision `0008` with `subscriptions` and
`subscription_price_periods` from the architecture.

- Cadence supports monthly, yearly, and fixed custom month interval.
- Price periods cannot overlap for the same subscription.
- Inactive/zero-price subscriptions remain visible but contribute zero.
- Annual/custom costs are allocated in minor units across contribution months.
  Remainders are distributed deterministically so the full cycle total is exact.
- Historical month calculations use prices effective in that month.
- Default funding wallet is the configured First Bank Subscription wallet, not
  a hard-coded account ID.

### Application/UI

- CRUD commands with optimistic concurrency.
- `CalculateSubscriptionRequirement(period)` returns item evidence and total.
- `/subscriptions` supports add, edit, activate/deactivate, and month preview.
- No JavaScript build step; HTMX partial updates are optional.

### Acceptance tests

- Monthly and annual examples retain exact total minor units.
- Price changes affect future periods but not frozen salary runs.
- Overlapping effective ranges fail atomically.
- TWD/JPY display without decimal; USD follows exponent.
- Disabled and zero-price rows do not disappear from history.

### Commit

Suggested message: `feat: add subscription requirement register`

Stop after B4 review.

### B4 implementation status and decisions

Status: **accepted at revisions `0008_subscriptions`,
`0009_subscription_intervals`, and `0010_subscription_interval_invariants`.**
Decisions taken while implementing this milestone, recorded as ADR-B033 to
ADR-B039 in `BUDGET_ARCHITECTURE.md`:

- Every activity and price boundary is the first day of a month, in the domain
  and in the database, and the price for a calendar month is the one covering
  that month's first day (ADR-B033). Without that rule "no daily proration"
  would still leave a month with two candidate prices. The four boundaries are
  `DATE` columns rather than `UTCDateTime` instants, because a calendar month
  is not a point in time.
- A yearly cadence requires `billing_month` and a custom cadence requires
  `active_from` as its cycle anchor, both enforced by a check constraint
  (ADR-B034). Requiring `active_from` is the minimal schema correction the
  milestone asked for: `billing_month` cannot anchor a cycle whose length does
  not divide twelve, and inventing an anchor from the current date is exactly
  what the brief forbids.
- `cadence_months` is constrained per cadence in the database as well as the
  domain: monthly is 1, yearly is 12, and custom is 2 to 120.
- Deactivating requires the month it stopped, enforced as
  `is_active = 1 OR active_to IS NOT NULL` (ADR-B036). The calculation reads
  the activity range only, so `is_active` can never rewrite history. Zero
  contributions always carry one of `not_yet_active`, `no_longer_active`,
  `no_price_for_month`, or `zero_price`.
- Non-overlap is validated by the service inside the writing transaction and
  again by BEFORE INSERT and BEFORE UPDATE triggers (ADR-B038). A trigger
  rejection is translated back into `SubscriptionOverlapError`, so a direct
  repository write reports the same stable failure as the service.
- Both `subscriptions` and `subscription_price_periods` update through a
  conditional `UPDATE ... WHERE version = :expected` with a row-count check.
  A price is corrected and versioned, never deleted.
- The default funding wallet is one `app_settings` row (ADR-B037). No
  institution code, account ID, wallet ID, or subscription name is hard-coded
  anywhere in B4.
- `CalculateSubscriptionRequirement` takes a read repository and returns one
  `SubscriptionRequirement` per currency; the record refuses to hold two
  currencies, so a total can never mix them. The CLI prints the exact minor
  units beside the formatted amount, because TWD and JPY display without
  decimals and the formatted value alone would hide a monthly share.
- `/subscriptions` is local-only and is not in the external allowlist, so the
  external hostname receives `404` for every read and write. Literal routes are
  declared before `/subscriptions/{subscription_id}` so `default-wallet` is
  never parsed as an ID.
- B4 creates no salary table and freezes nothing. The "price changes do not
  affect a frozen salary run" acceptance test belongs to B5; B4 tests the
  historical and future effective-date calculation that B5 will freeze.

Applied after the first B4 review:

- A subscription that already has a price can no longer change the
  configuration that price is read through (ADR-B040). Editing currency and
  cadence reread the same stored `99900` as another currency on another cycle,
  so a month calculated earlier silently changed its amount, and moving the
  category or wallet reattributed history that had already been recorded.
  `name`, `billing_day`, and the activity range stay correctable; the page
  renders the fixed values as text rather than offering a control the service
  would refuse.
- Reactivation is removed (ADR-B036, amended). Clearing `active_to` deleted
  the fact that the subscription ever stopped and made every month in the gap
  contribute again. `resume` now creates a new subscription interval starting
  on or after the month the old one ended, and never touches the stopped row,
  so use, stop, and use again are three facts the register shows at once.
  `UpdateSubscriptionRequest` no longer carries `is_active`, and a deactivated
  row cannot clear its end month through an ordinary edit either.
- The `subscriptions.name` unique index is translated into
  `SubscriptionError` in the repository, on insert and on update. The service
  checks the name first, but that check and the write are two statements, so
  the race had to stop leaking a SQLAlchemy traceback through the CLI. The web
  layer returns a stable `422` and the whole transaction rolls back.

Applied after the second B4 review:

- Revision `0009_subscription_intervals` replaces the unique index on the
  active name with a real non-overlap invariant over the intervals of one
  service (ADR-B041). The index released the name as soon as an interval was
  deactivated, even with its end month still in the future, so a second
  interval could cover the same months and the requirement counted the service
  twice. Revision `0008` is not modified; `0009` drops that index, adds a
  `(name, active_from, active_to)` index, and adds BEFORE INSERT and BEFORE
  UPDATE triggers.
- The invariant is checked by the service on every mutation that can move an
  interval — create, edit, deactivate, and resume — and always against every
  interval the service has held, not only the one a resume started from.
  Renaming an interval onto an overlapping service and extending one interval
  into the next are both refused.
- Both bounds are optional in the comparison: a missing `active_from` means
  "since always" and a missing `active_to` means "still running", so an
  unbounded interval leaves no room on that side. Adjacency stays legal.
- The active-name check applies only to a row that is itself running, which
  fixes an inactive interval becoming uneditable after a resume: a later
  interval legitimately holds the same name, and the old row only has to avoid
  overlapping it.

Applied after the third B4 review:

- Revision `0009` is left exactly as it was published, and revision
  `0010_subscription_interval_invariants` carries the correction. Rewriting an
  applied revision leaves any database that already ran it with no migration
  path to the fixed schema: Alembic sees the revision as current and never runs
  the corrected code. A defect in a released migration is repaired by the next
  revision, never by editing it in place.
- Revision `0010` no longer drops 0008's unique index on the active name. The
  two invariants are independent — two stopped intervals can overlap without
  touching the index, and two adjacent intervals can both be running without
  overlapping — so `0009` adds its triggers beside the index instead of
  replacing it. That also removes a state the new schema could hold and the old
  one could not, which had made the downgrade fail on legal `0009` rows with a
  `UNIQUE constraint failed` error.
- `ManageSubscriptions` enforces the active-name rule as well, so the service
  never produces a row the index then refuses, and the repository translates
  that constraint back into a stable error for the concurrent case.
- `0010` runs from either shape a database at `0009` can have: it creates the
  active-name index when the published `0009` dropped it, and verifies the
  definition rather than recreating it when the index is already present. An
  index of that name that `0010` did not install is refused instead of being
  accepted silently. Its downgrade returns the schema `0009` was published
  with.
- `0010` refuses to upgrade at all when existing rows already break either
  invariant — overlapping intervals, or two intervals of one name running at
  once. A
  trigger cannot validate rows written before it existed, so the pre-flight
  check runs before any DDL and aborts the migration: the transaction rolls
  back, the revision stays `0008`, and the index, triggers, and rows are
  unchanged. The error reports only subscription ids, never a name or amount,
  and asks for the correction to be made at `0008`, because which interval of
  an overlapping pair is the right one cannot be guessed.
- Because `0009` installs no guard, an illegal `0008` database now stops in
  `0010`; every pending migration runs in one transaction, so it rolls all the
  way back to `0008`. A test asserts exactly that.
- The migration round-trip test now seeds real subscription rows — adjacent
  intervals, one stopped and one running, plus an unbounded one — and compares
  full row snapshots, indexes, triggers, and the revision across
  `0008 -> 0009 -> 0008 -> 0009`.

## 11. Budget Milestone B5 — Salary allocation

### Goal

Generate a versioned monthly salary distribution, including computed
Subscription funding, same-account wallet moves, cross-bank commitments, and
manual **已轉帳** confirmation.

### Schema

Implement revision `0011` (with `0012` added after the first review):

- `salary_plan_templates`
- `salary_plan_template_items`
- `salary_allocation_runs`
- `salary_allocation_run_items`

Run items freeze name, destination, calculation mode, amount, and evidence.
Template edits never mutate approved or historical runs.

### Behavior

- Select an existing posted salary-income event, or use the narrow
  `RecordSalaryReceipt` action to post the actual deposit into the configured
  Cathay salary account and `Unallocated` wallet before generating the run.
  B7 later provides the general bank-movement UI.
- Generate one draft run per template/period.
- Calculation modes: fixed, subscription requirement, manual.
- Planned total cannot exceed salary without explicit validation failure.
- Remainder is assigned to the salary account's `Unallocated` wallet.
- Approving a same-account allocation posts an internal wallet reclassification.
- Approving a cross-bank allocation creates a commitment only.
- User pressing **已轉帳** records the real account-to-account journal movement
  and completes the corresponding run item.
- Retrying **已轉帳** cannot post twice.

### Application/UI

- Template setup/edit services and `/allocations/{yyyy-mm}`.
- Generate, preview, approve, cancel-draft, and mark-transferred actions.
- Show source calculation for Subscription rather than a copied number.
- Show item status: draft, reserved, waiting transfer, transferred, error.

### Acceptance tests

- Template changes do not alter an approved run.
- Recording salary receipt increases the actual salary account exactly once.
- Same-bank allocation preserves bank total and changes wallet totals.
- Cross-bank approval changes availability but not actual destination balance.
- **已轉帳** changes both real bank balances exactly once.
- Salary remainder is exact and visible.
- The current wallet topology places childcare insurance at Taiwan Business
  Bank, never First Bank.

### Commit

Suggested message: `feat: add monthly salary allocation`

Stop after B5 review.

### B5 implementation status and decisions

Status: **accepted at revisions `0011_salary_allocations` and
`0012_salary_regeneration_provenance`.**
Decisions taken while implementing this milestone, recorded as ADR-B042 to
ADR-B048 in `BUDGET_ARCHITECTURE.md`:

- A template describes the plan for *future* months; every generated month
  freezes its own snapshot, so editing a template can never change a run that
  already exists (ADR-B042). Template items are not effective-dated, because
  the run snapshot is what actually decided money and a second dating
  dimension could disagree with it. Adding, editing, or deactivating an item
  raises the template's version in the same conditional statement.
- A cross-account line is the commitment (ADR-B043). `funding_commitments`
  is untouched — it is bound to a `financial_event` plus an
  `event_allocation`, which a salary line has neither of — and no fifth table
  is created. `reserved_minor` now sums open funding commitments *and* waiting
  salary lines, each exactly once.
- No `FinancialEventType.SALARY` is added, so `financial_events` is not
  rebuilt (ADR-B044). A salary is `manual` / `deposit` / `bank_account` /
  `observed_manual`, and revision `0011` inserts `salary_receipt` and
  `funding_transfer` into the `journal_entry_types` registry. A same-account
  wallet move reuses `internal_transfer`.
- B4's cadence and rounding logic is extracted into a pure
  `calculate_requirement(inputs, ...)`, shared by the read-only preview and by
  generation (ADR-B045). Generation reads the register through its own Unit of
  Work, so the amounts it freezes come from the snapshot it is written into.
  The run stores every requirement item, including zero contributions and
  their reason codes, as canonical JSON with integer amounts only.
- Approval reclassifies wallets for a same-account destination and reserves
  everything else (ADR-B046). Two accounts at one institution are two real
  accounts; the decision reads `financial_account_id`, never a bank name.
  Approval checks that the source `Unallocated` wallet's *available* balance —
  ledger balance minus existing reservations — covers the whole plan.
- The real movement is posted only when the user presses **已轉帳** and states
  when it happened (ADR-B047). No `scheduled_transfers` row, no worker, and no
  06:00 assumption: those belong to B6.
- One template and month hold at most one non-cancelled run, and one salary
  event feeds at most one (ADR-B048). Regenerating cancels the current draft
  and writes generation + 1 in the same transaction, so a failed generation
  leaves the old draft untouched. An approved run is never regenerated.
- `RecordSalaryReceipt` is the one narrow bank-income entry point B5 owns; the
  general deposit and withdrawal surface stays in B7. Its idempotency compares
  account, amount, currency, and occurred-at, and deliberately ignores the
  note, which is display text and does not change the accounting effect.
- Frozen snapshot columns are protected twice: the service only ever writes
  status, entry links, timestamps, and version, and revision `0011` adds
  BEFORE UPDATE triggers that abort any statement changing a frozen column,
  whichever path reaches the table.
- The `0011` downgrade refuses, before any DDL, when salary rows exist or when
  the journal already records a `salary_receipt` or `funding_transfer` entry.
  Dropping those tables would delete a recorded plan and removing the registry
  rows would orphan a posted entry, so the refusal reports counts only — never
  a template name or an amount — and leaves revision, schema, and data
  unchanged.
- The unit tests live in `tests/unit/test_salary_rules.py` rather than
  `test_salary_allocation.py`, because mypy refuses two test modules with the
  same basename and the integration suite already owns that name.

Applied after the first B5 review, which found two P1 correctness defects and
reproduced both (ADR-B049):

- A salary idempotency token could be occupied by any event with the same
  money. `RecordSalaryReceipt` compared amount, currency, time, and one
  posting, so a Gmail-origin card purchase posted as an `adjustment` replayed
  as a recorded salary. The replay now validates the requested amount through
  the domain's integer validator before comparing anything — otherwise `True`
  and a float compare equal to a stored integer — and then re-verifies the
  event's whole identity and the entry's exact two postings, income account,
  link, and effective time. Anything else is an `IdempotencyConflictError`.
- A regenerate replay identified its own result by "a later live generation of
  this month", which is not the same thing. Revision `0012` adds
  `regenerated_from_run_id` and `request_fingerprint`, so a replay matches on
  provenance *and* payload; a generation produced by the ordinary path carries
  neither and can never be mistaken for one, and the same provenance with a
  different payload is a conflict.
- Revision `0011` is left exactly as published and `0012` carries the
  correction, because the user's database had already run `0011`. B6's
  scheduled transfers therefore become revision `0013`.
- `0012` rebuilds the runs table under the ADR-B027 protocol instead of using
  `ALTER TABLE ADD COLUMN`. SQLite accepts an added column with an inline
  `REFERENCES` clause, but stores it in a form whose `ON DELETE` SQLAlchemy's
  reflection does not recover, which left `alembic check` reporting a
  permanent phantom difference; a table-level foreign key reflects identically.
- The test that pinned the old regenerate behaviour is corrected: it replayed
  without passing the manual amounts and still expected success, which
  contradicted ADR-B048's "identical request".

## 12. Budget Milestone B6 — Clearing and scheduled assumptions

### Goal

Implement First Bank `帳單暫存`, scheduled funding transfers, card autopay,
06:00 assumptions, catch-up, cancellation, and reversal without Redis.

### Schema

Implement revision `0013`:

- `scheduled_transfers` with purpose, source/destination ledger accounts,
  optional wallets, amount/currency, local due date/time/timezone, UTC due time,
  state, policy, worker claim, executed entry, processed/error fields, version.
- `scheduled_transfer_commitments` to cover one or more commitments with exact
  amounts.

Use ledger-account destinations so a schedule can fund another bank asset or
clear a card liability. Asset destinations require a wallet.

### State and worker

- States: scheduled, processing, assumed_executed, cancelled, failed, reversed.
- Cancel only before execution; correct executed schedules by reversal.
- Build `LocalMaintenanceCoordinator`, separate from `LocalSyncWorker`.
- Run startup catch-up and bounded polling while `serve` is alive.
- Claim due rows atomically with lease owner/expiry and bounded batch size.
- Unique execution idempotency key prevents double posting after a crash.
- If Mac wakes late, retain due time as `effective_at` and now as `processed_at`.

### Policies

- First clearing transfers default to 06:00 Asia/Taipei and display
  **已自動視為轉出**.
- Card autopay also uses an explicit scheduled amount/date and configurable time,
  default 06:00.
- Scheduled assumption is not bank confirmation.
- Cross-bank salary allocations remain manual and do not use this worker.

### Application/UI/CLI

- `/clearing` commitments, grouping, schedule creation, cancellation, reversal,
  and execution history.
- Card detail supports autopay schedule.
- `python -m app money run-due --dry-run` and apply mode.
- Add bounded maintenance settings with safe defaults.

### Acceptance tests

- Before due: reserved changes, actual balance does not.
- At/after due: one balanced entry and correct commitment coverage.
- Two coordinators cannot claim the same transfer.
- Crash/retry cannot double-post.
- Late catch-up preserves effective and processed timestamps.
- User-visible copy never says bank-confirmed.
- Maintenance failure does not stop Gmail sync or offline Dashboard.

### Commit

Suggested message: `feat: add local clearing scheduler`

Stop after B6 review.

### B6 implementation status and decisions

Status: **implemented at revision `0013_scheduled_transfers`; awaiting
review.** Decisions taken while implementing this milestone are recorded as
ADR-B050 to ADR-B060 in `BUDGET_ARCHITECTURE.md`:

- A schedule is a local policy assumption, never a bank confirmation
  (ADR-B050). Every surface says **已自動視為轉出** and **來自本機排程政策，
  並非銀行確認**, and a route test asserts no page ever claims otherwise.
- One table carries two posting shapes (ADR-B051). A clearing transfer posts
  `funding_transfer` between two asset wallets; a card autopay posts the new
  `card_payment` type against a card liability. A named check constraint makes
  the two shapes mutually exclusive in the row itself, so a `clearing_funding`
  row cannot hold a card and a `card_autopay` row cannot hold a destination
  wallet.
- `scheduled_transfer_commitments` lets one real transfer settle several
  reservations (ADR-B052). The links are immutable — UPDATE and DELETE both
  abort — and a BEFORE INSERT trigger refuses coverage beyond
  `amount_minor - released_minor`, counting only schedules that are neither
  cancelled nor reversed.
- Economic columns freeze on insert (ADR-B053) and a BEFORE UPDATE trigger
  enforces it beside the service, so only lifecycle, lease, error, version, and
  timestamp columns can move. Correction is cancel plus replacement.
- An executed schedule is corrected by reversal only (ADR-B054).
  `JournalService` gains `stage_reversal`, so the reversal entry, the
  schedule's new status, and every affected commitment commit together; the
  scheduled-transfer service never reaches the journal repository.
- Execution ownership is one conditional `UPDATE` plus an expiring lease
  (ADR-B055). Claiming commits on its own, each schedule executes in its own
  Unit of Work, and completing re-checks status, lease owner, *and* version.
  `scheduled_transfer:execute:{id}` and `scheduled_transfer:reverse:{id}` make
  a retry a replay rather than a second posting.
- `LocalMaintenanceCoordinator` is separate from `LocalSyncWorker` (ADR-B056):
  its own daemon thread, stop event, bounded poll and batch, startup catch-up,
  and idempotent start/stop. One schedule failing never stops the batch, and a
  maintenance failure never touches Gmail sync or the Dashboard.
- B5 salary lines are never scheduled (ADR-B057). A waiting line reserves
  through its own status and has no commitment row, so there is nothing for a
  schedule to select — the rule holds by construction, not by a filter.
- `/clearing` and all six POST routes stay local-only (ADR-B058); the external
  hostname answers `404` for every one of them.
- `reserved_minor` is rewritten as three disjoint parts (ADR-B059): waiting
  salary, the uncovered part of each commitment's requirement, and what each
  active schedule will really debit. It reads amounts only, never a
  commitment's `status` column, so a stale status cannot change an available
  balance. One pure `derive_commitment_status` is the only place any service
  decides a commitment's status.
- A refund never rewrites a scheduled bank movement (ADR-B060). The excess
  stays reserved, the page warns, and the only offered correction is cancel
  plus replacement.

Two further notes for the reviewer:

- `test_approval_creates_no_scheduled_transfer_table_row` in the B5 suite
  asserted that the `scheduled_transfers` tables did not exist. Revision `0013`
  creates them, so the assertion is now that a salary approval writes *no rows*
  into them, which is the property ADR-B057 actually needs.
Applied after the first B6 review, which found three P1 correctness defects
and reproduced all three against an isolated SQLite database:

- **The refund path never re-derived commitment status.**
  `AllocateFinancialEvent` called `release()` without `refresh_status()` in the
  same Unit of Work, so a commitment of 8,000 partly scheduled at 5,000 and
  then refunded by 4,000 kept reading `partially_scheduled` even though the
  existing coverage now covered its remaining 4,000 in full. The B6 refund test
  had been calling `refresh_status` by hand afterwards, which hid the gap in
  the real path; that manual call is removed and the service now refreshes
  exactly the commitments it released, before committing.
- **A split schedule lost the over-coverage warning.** `excess_minor` compared
  each link with its commitment's requirement one at a time, so a commitment of
  8,000 covered by two schedules of 4,000 and then refunded by 4,000 computed
  `max(4,000 - 4,000) = 0` twice and warned about nothing while 8,000 was still
  on its way to the bank. Over-coverage is now aggregated per commitment across
  every `scheduled`, `processing`, `failed`, and `assumed_executed` link, the
  page-level warning reads that aggregate rather than the rows currently on
  display, and the clearing page lists the affected commitments with their
  requirement, their total coverage, and the surplus.
- **Resending a reversal was not idempotent.** `reverse()` ran the state
  transition check before looking for an existing reversal, so an identical
  HTTP or CLI resend hit `reversed -> reversed` and failed; the stable reverse
  key never got the chance to replay, and the user who most needed to know the
  reversal had worked was shown a failure. The key is now consulted first: a
  replay re-verifies the stored entry's key, type, and reversed target, returns
  `created=False`, and every other terminal state is still refused.

Applied after the second B6 review, which accepted the three fixes above and
found two more defects introduced by the over-coverage aggregate itself:

- **Cancelled and reversed history rows were blamed for a live surplus.**
  `has_refund_warning` read the commitment's global live coverage without
  asking whether *this* schedule was still live, so after two 4,000 schedules,
  a 6,000 refund, and cancelling one of them, the remaining schedule correctly
  showed 2,000 while the cancelled row showed it too — and the template went
  further and told that row the money had already moved. A row now contributes
  nothing once it is `cancelled` or `reversed`, and the correction offered is a
  stable code per state: `cancel_and_replace`, `wait_for_run` for a claimed
  row that has moved nothing yet, and `reverse` only for one that really has.
- **The over-covered count and list were truncated by the history limit.**
  `over_covered_commitments` used the shared history limit while
  `ClearingView.over_covered` returned the length of what came back, so 26
  over-covered commitments displayed as 25 with no page reaching the 26th. The
  repository now returns rows plus a database `COUNT`, and the warning list has
  its own `warn_page` pagination with the shown range and a link to the rest.

Applied after the third B6 review, which accepted the row-level gating and the
exact count, and found two defects in what the page built on top of them:

- **The summary advised a correction the rows could not perform.** It always
  told the user to cancel the un-executed schedules, but a surplus can be
  carried entirely by `processing` schedules, which can only be waited out, or
  by `assumed_executed` ones, which can only be reversed — directly
  contradicting the correct per-row advice underneath. The summary is now
  aggregated from the real statuses of every over-covered commitment's live
  schedules, in a fixed order, and lists only the corrections that are actually
  available.
- **An out-of-range warning page displayed a nonsense range.** `warn_page` was
  never normalized against the total, so `?warn_page=1000` over 26 rows
  produced an empty page claiming to show rows 0 to 24,975 with only
  page-by-page navigation back. Both lists now clamp the offset to the last
  page that exists, report their page number and page count, and carry the
  other list's page parameter in every link.

The mutation runner itself had a defect worth recording. It rewrote one source
file per mutation and Python validates a `.pyc` by source mtime and size at
one-second granularity, so two mutations of the same file that removed the same
number of bytes within one second could reuse the first one's bytecode. That is
how `reversing stops refreshing the commitments it freed` once reported itself
as survived when the mutation was in fact caught. The runner now purges cached
bytecode and runs with `PYTHONDONTWRITEBYTECODE=1`; the whole suite is 25/25.

- The `destination.kind` guards in the posting builder are defence in depth
  with no reachable path through the service API — a clearing transfer to a
  card liability is refused earlier, because a liability account has no wallets
  and a clearing schedule requires a destination wallet. That refusal is what
  the test suite pins; the guards themselves are excluded from the mutation
  scope for the same reason.

## 13. Budget Milestone B7 — Daily activity, manual expenses, and reconciliation

### Goal

Deliver the everyday workflow approved in the Prototype: browse by day, filter
cash/card, add either payment type, record bank movements, link later Gmail
notifications, and reconcile bank balances.

### Schema

Implement revision `0014` with `balance_snapshots`. Add columns/indexes only if
the B2 source-link schema and query measurements prove they are needed.

Required indexes include:

- financial event `(occurred_at, payment_method, status)`;
- source link `(transaction_id, status)` and `(financial_event_id, status)`;
- snapshot `(financial_account_id, observed_at)`.

### Manual cash expense

- Required: occurred time, merchant/purpose, nonzero amount, currency, category,
  cash account/wallet, client idempotency token.
- Post expense against cash asset immediately and atomically.
- No fake Email, parser, confidence, or Gmail label operation.

### Manual credit-card expense

- Required: occurred time, merchant/purpose, amount/currency, category, card,
  funding wallet, idempotency token.
- Post expense and card liability immediately; create funding reservation or
  commitment using the same B3 rules.
- Mark origin/manual and payment method/credit card.

### Later Gmail matching

Run matching before creating a new Gmail-origin event:

1. Candidate must have exact currency and amount.
2. Compatible card identity is required when both observations know the card.
3. Transaction type must be compatible.
4. Occurred times must be within a configured, tested window (initially 72h).
5. Exact normalized merchant and a tighter time window strengthen confidence.
6. Auto-link only one unique candidate meeting a documented threshold and all
   mandatory fields.
7. If several candidates qualify or evidence is weaker, persist candidates and
   show review. Do not create/post a duplicate event while review is pending.
8. User can confirm one link, reject candidates, or declare a separate expense.
9. Linking attaches provenance; it does not create a journal entry.

Use integer score basis points and structured reason codes. The initial scorer
is deterministic: exact card 3000, compatible event type 2000, time within 24h
2500 (or within 72h 1000), exact normalized merchant 2500 (or conservative
token match 1500). Amount/currency are gates and add no points. Auto-link only a
single 10000-point candidate. All lower scores require review. Normalize with
Unicode NFKC, case-folding, punctuation removal, and whitespace collapse; do
not add a fuzzy-matching dependency. Merchant similarity alone can never link.

### External bank movement

- One form handles deposit or debit against a selected account/wallet.
- Known event types include interest income, direct debit, fee, refund, external
  transfer, and adjustment.
- The Taiwan Business Bank childcare-insurance example directly reduces its
  configured wallet; it never passes through First Bank or a card.
- A consumed-cash withdrawal is a distinct observed movement: debit the chosen
  expense category, credit the explicitly selected checking/savings wallet,
  report it as cash in the daily/monthly allowance, and do not increase a
  cash-on-hand account. This supports the user's convention of recording the
  ATM withdrawal once instead of entering individual cash tips later. A plain
  external withdrawal remains non-spending.
- Unknown wallet uses explicit `Unallocated`.
- Generic adjustment requires a reason and is reserved for reconciliation.

### Reconciliation

- Store observed bank-app balance and timestamp.
- Query ledger balance at the same instant and display signed difference.
- Snapshot never changes the ledger.
- Resolution is a missing known event or explicit reasoned adjustment.

### Daily UI

- `GET /money/daily?date=YYYY-MM-DD&payment_method=...`
- Default date is today in configured timezone.
- Date strip navigates at least the surrounding seven days without horizontal
  page scrolling.
- Filters: all, cash, credit card.
- Group/order within a day is newest first.
- Show time, merchant, amount/currency, category, payment identity, and status.
- One **新增花費** action branches to cash or credit-card fields.
- Mobile bottom navigation exposes Daily as a primary item.
- POST success uses Post/Redirect/Get; validation preserves safe user input.

Suggested routes:

```text
GET  /money/daily
GET  /money/expenses/new
POST /money/expenses
POST /money/bank-movements
GET  /money/source-links
POST /money/source-links/{id}/confirm
POST /money/source-links/{id}/reject
GET  /money/reconcile
POST /money/reconcile
```

All remain local-only and reuse existing CSRF/origin validation.

### Acceptance tests

- Daily totals split cash/card and do not mix currencies.
- Time ordering is newest first in Asia/Taipei.
- Cash and credit-card forms post the correct two ledger sides.
- Repeated form submission does not duplicate.
- A later exact Gmail match links without a second journal entry.
- Ambiguous same-amount purchases require review.
- Rejecting a candidate permits a distinct event intentionally.
- Interest and direct debit update only the selected account/wallet.
- Snapshot discrepancy is correct without mutating history.
- Mobile viewport has no horizontal scrolling.

### Commit

Suggested message: `feat: add daily spending and reconciliation`

Stop after B7 review.

### B7 implementation record

Delivered as planned, with these deliberate deviations from the sketch above,
each recorded as an ADR in `BUDGET_ARCHITECTURE.md`:

- **Routes.** The suggested set was extended rather than replaced:
  `GET /money`, `GET /money/events/{id}`, `POST /money/events/{id}/reverse`,
  `GET /money/accounts`, `GET /money/accounts/{id}`,
  `GET /money/bank-movements/new`, `POST /money/account-transfers`,
  `POST /money/refunds`, `POST /money/source-transactions/{id}/separate`,
  `POST /money/reconcile/snapshots`, and `POST /money/reconcile/{id}/adjust`.
  The daily page takes `?day=` and `?method=` rather than `?date=` and
  `?payment_method=`, matching the shorter query strings the other pages use.
  Every one of them is local-only (ADR-B068).
- **Generic adjustment is not a bank movement.** The sketch listed `adjustment`
  among the bank-movement types; B7 reserves it for reconciliation alone and
  gives interest, external deposits and withdrawals, direct debits, fees, bill
  payments, tracked transfers, and refunds their own real posting shape
  (ADR-B065). `BankMovementKind` therefore has no `adjustment` member at all.
- **Daily totals come from expense postings**, not from event magnitudes
  (ADR-B064), which is what makes refunds subtract, reversals subtract at their
  own effective instant, and multi-category splits exact. A reversal entry has
  no event of its own, so the query follows `reversal_of_id` back.
- **The daily page covers cash and card only.** A bank-paid direct debit is
  still spending, but it belongs to the account page; the three filter pills are
  all/cash/card, so "all" means exactly those two instruments.
- **`AllocateFinancialEvent` grew `stage(unit, request)`.** `execute` is
  unchanged for every existing caller; the new method is what lets a manual
  expense create its event and allocate it in one transaction (ADR-B061).
- **Matching runs before Gmail intake creates anything** (ADR-B062), and no
  "one confirmed link per event" constraint was added (ADR-B063).
- **Reversal refuses a deposit a live salary run was generated from.** Found
  while designing the mutation set: a salary receipt is a posted manual event
  like any other, so without the guard B7 could have cancelled the deposit a
  whole frozen month was calculated from.
- **Amounts are typed in display units on the web** and parsed by a pure
  `parse_display_amount` (Decimal, never float); the CLI keeps explicit
  `--amount-minor` so a script can never be ambiguous.

Revision `0014_reconciliation` adds `balance_snapshots`, its
`(financial_account_id, observed_at)` index, the two immutability triggers, the
`income` and `external_movement` registry rows, and
`ix_financial_events_matching_candidates`. The B2 source-link indexes and the
B7-era `ix_financial_events_daily_activity` index already existed and were left
exactly as they were.

Verification evidence for this milestone is in the handoff report: ruff, ruff
format, mypy, the full pytest run with coverage, a targeted `alembic check`, a
41-mutation run, and the rehearsed formal-database upgrade.

#### Review round 1 — five correctness defects, all fixed

An independent review reproduced five data-correctness defects through the
public service API. Every one of them was a test gap rather than a disagreement
about the design, and each is now closed with a regression test and a mutation:

1. **Idempotent replay did not compare where the money went.** The bank
   movement, transfer, and refund replays compared the event's own columns
   only, so one token could report success while the stored rows put the money
   in a different wallet, under a different category, or ran a transfer the
   other way. Every replay now re-derives the canonical postings and allocation
   from its own request and compares them, plus the entry's type, instant,
   event link, and key (ADR-B067).
2. **A card notification with no last four created a duplicate.** Matching
   required the source to be recognized as `credit_card`, and a missing
   last-four resolves to `other`, so those notifications skipped matching
   entirely. Only an explicit *bank-account* notification is excluded now; a
   card notice whose card is unknown is compared, reaches at most 7,000 bps,
   and goes to review (ADR-B062). The test that had frozen the wrong behaviour
   was corrected: its fixture now carries a real `account_last_four`.
3. **A snapshot could be taken against a card liability.** Such an account
   holds no wallets, so the adjustment could never post, and the snapshot was
   permanent by design. Snapshots are now restricted to asset accounts with an
   `Unallocated` wallet, before the row is written, and the reconciliation page
   offers only those accounts (ADR-B066).
4. **One adjustment token answered for any snapshot.** The key is now
   `reconciliation_adjustment:{snapshot_id}:{token}`, so a resubmission aimed
   at a second observation can never be answered with the first one's
   adjustment. No schema change was needed: the binding is in the key the
   event already stores, and the journal key already names the event.
5. **The adjustment replay did not verify the stored entry.** It now checks the
   payment method, recognition basis, status, the entry's event link and key,
   and both postings' full signature.

Two acceptance gaps were closed at the same time: a hand-entered expense now
**requires** a category in the request, the CLI, and the form; and the expense
POST decodes the payment method strictly — an unknown value is refused rather
than silently posted as cash — and a rejected submission re-renders the form
with everything the user safely typed instead of a bare sentence.

#### Review round 2 — canonical replay completed

The first repair still had two branch-specific holes. A bank-paid expense
validated its allocation but skipped the journal key and posting signature;
a refund validated only its allocation total. Both could therefore answer a
retry with success while the journal or category evidence described something
else. Allocated movements now validate all three layers every time: canonical
event identity, exact allocation identity, and the entry key plus complete
posting signature. Refunds replay the deterministic sequence of partial-refund
splits, so a later partial refund is checked against the remainder that existed
when it was created rather than only against an aggregate amount.

Replay also no longer applies today's active-state policy to yesterday's fact.
New movements still require active wallets and accounts, but an identical retry
can resolve the historical identities after either is deactivated and return
the original entry. The reconciliation read model now identifies the
`Unallocated` envelope by `WalletPurpose.UNALLOCATED` and an asset ledger, not
by the mutable display name.

#### Follow-up — editable notes on every daily event

The original B7 form accepted a note only while a manual expense was created.
The event detail could display that value, but could neither add nor edit one
for a Gmail-origin event, and the daily list hid it. The completed follow-up
adds one optimistic `save_note` operation over the existing
`financial_events.note` column, a local-only POST on the event detail, and the
same note on each daily card. Empty text clears the note. This is deliberately
not a journal edit: entries, postings, allocations, commitments, balances, and
source evidence remain byte-for-byte unchanged. Parser-owned source refreshes
still update only their existing structured fields and preserve the user note
(ADR-B074). No migration is required.

## 14. Budget Milestone B8 — Consolidated Dashboard

### Goal

Make the implemented vertical slices feel like one Scribe product, matching the
approved Prototype and preserving current Gmail/report workflows.

### Information architecture

Desktop navigation:

1. 總覽
2. 每日收支
3. 帳戶與小錢包
4. 本月預算
5. 薪資分配
6. 帳單暫存
7. 訂閱管理
8. 卡片設定
9. Existing transactions/reports/sync administration

Mobile primary navigation:

- 總覽
- 每日
- 預算
- 帳戶
- 更多: salary allocation, clearing, subscriptions, cards, administration

### Page requirements

- `/money` shows actual/reserved/available by currency and reconciliation alerts.
- Account cards show bank name, wallet count, balance, reservations, and status.
- Budget view distinguishes remaining target from intentional buffer.
- Salary view shows calculated source and **已轉帳** actions.
- Clearing view labels assumptions precisely.
- Subscription view recalculates monthly requirement after edits.
- Card view always shows bank name beside last four.
- Empty/loading/error states are server-rendered and user-readable.

### Responsive requirements

- No table is allowed to force horizontal page scrolling on a 360px viewport.
- Convert dense tables to stacked transaction/account rows on mobile.
- Controls have at least a practical touch target and visible focus state.
- Money and status stay legible without truncating currency.
- Test 360px, 390px, 736px, and desktop widths.
- No CDN calls; continue vendored HTMX/Chart.js and no build pipeline.

### Existing-route compatibility

- For local requests, `/` redirects to or renders the new `/money` overview.
- For the validated external hostname, `/` continues rendering the current
  external-safe recent-transactions overview.
- Existing transaction browsing remains available at `/transactions`.
- External `/` and `/reports` behavior must not expose balances.
- Existing external transaction confirmation must continue to work unchanged.

### Acceptance tests

- Every page is reachable through local navigation.
- External Host receives 403/404 for all money routes and POSTs.
- Existing routes and transaction confirmation tests remain green.
- Mobile screenshots match approved hierarchy and have no horizontal overflow.
- Dashboard can start and render all cached money data without Gmail/network.

### Commit

Suggested message: `feat: integrate budget dashboard`

### Implemented B8 decisions

- No schema revision is needed. `ConsolidatedMoneyQueries` composes the
  existing account, daily, budget, source-link, and parsed-transaction reads.
  It groups asset balance, reservation, available money, and signed card
  liability per currency; it never folds debt into availability or adds
  currencies.
- Local `/` and `/money` render the consolidated overview. The external Host
  takes the old dashboard branch and never receives the consolidated view.
  Recent Gmail notifications and bulk confirmation remain on the local page,
  so adopting the ledger dashboard does not remove the earlier review flow.
- Desktop navigation follows the approved information architecture. A fixed
  five-item mobile bar appears at 760px and below, with `/more` as the bounded
  hub for lower-frequency money, Gmail, report, and configuration tasks.
- Budget tables are labelled responsive cards at phone width. Account cards
  distinguish assets from card liabilities, show wallet counts and evidence
  status, and card configuration resolves the sender-rule institution name
  beside the last four digits.
- `build_web_application` gives FastAPI ownership of the shared database
  engine's close callback; lifespan shutdown disposes it after maintenance
  stops, closing the known Web test connection leak at the integration seam
  where it belongs.
- Final evidence: 1,305 tests passed with 89.93% coverage, the full
  `-W error::ResourceWarning` run had zero warnings, Alembic autogenerate found
  no schema operations, and browser measurements from 360 through 1440px found
  no page-level horizontal overflow. Revision 0014 remained unchanged; B8 did
  not write or migrate the formal database.
- B8 review follow-up: the B6 clearing tables no longer satisfy phone layout by
  merely living in an internal horizontal scroller. At 760px and below, every
  wallet, commitment, warning, and schedule row becomes a labelled card. This
  keeps labels horizontal and values readable instead of squeezing eight table
  columns into one-character-wide strips. The desktop table remains unchanged,
  and clearing checkboxes use the existing custom control with a 44px label
  target.

Stop after B8 review.

### Follow-up — dedicated-wallet card spending

Some card purchases consume a purpose-specific wallet rather than the
calendar month's general spending allowance. Revision `0015` records that
choice per allocation in immutable `dedicated_wallet_allocations` evidence.
The purchase still debits its expense category and credits the card liability,
but the same entry also reduces the source envelope and increases the selected
`clearing_hub` wallet inside the same physical account. Its commitment is then
funded by the clearing hub. These entries remain visible in event and account
history but are excluded from `/money/daily`, daily totals, budget actuals, and
month-close spending calculations. Refunds inherit the same route and move the
money back. Cash, mixed treatment, non-hub destinations, and cross-account
virtual moves are rejected rather than guessed.

Implementation evidence: the full suite passed 1,330 tests with 89.93% total
coverage, including a run that treats every `ResourceWarning` as an error.
The 0014 → 0015 → 0014 → 0015 rehearsal preserved 1,172 emails, 1,286 parsed
transactions, 615 attachments, 14 canonical events, and every existing
journal row. Browser measurements at 360px and 390px found no page-level or
element-level horizontal overflow and no visible interactive target below
44px. The formal database was backed up before being upgraded to 0015; no
sample financial row was inserted.

#### Follow-up — confirmation-to-allocation workflow

Past Gmail transactions no longer end their local workflow at “confirmed”. A
detail confirmation redirects to that event's `/money/review` form, while a
bulk confirmation redirects to the complete allocation queue. Matching still
takes precedence: an ambiguous source goes to source-link review, and an exact
match opens the existing event instead of asking the user to allocate a second
copy.

The allocation page presents two separate actions rather than one mode selector:

- **列入每月生活費** uses the normal category/funding allocation and counts in
  the transaction month's daily-life budget;
- **從專款小錢包扣除** is card-only, deducts the full amount from one envelope,
  adds the same amount to `帳單暫存`, and stays outside the monthly-life total.

The dedicated form is built per account owning an active `clearing_hub`, and
lists only active non-hub wallets from that exact physical account. In the
approved production topology this means First Bank only. This is also enforced
inside `AllocateFinancialEvent`, so changing a wallet id in HTML cannot move
money from another bank or a cash account. The externally exposed Cloudflare
confirmation route ignores the continuation request and returns to the safe
external overview; no money configuration is rendered externally. No schema
revision is required because revision 0015 already stores the immutable
dedicated route.

Verification for this follow-up: 1,341 tests passed with 89.94% total
coverage, including the full suite with `ResourceWarning` promoted to errors;
Ruff, Ruff format, mypy, and `git diff --check` were clean. Route tests cover a
single local continuation, a bulk continuation, external refusal to continue,
and the rendered dedicated form excluding a wallet from another physical
account. Revision 0015 and the formal database were not changed.

## 15. Budget Milestone B9 — Cutover, import, backup, and hardening

### Goal

Safely establish Scribe as the operational source of truth using reviewed local
configuration/import data, then prove recovery, privacy, and performance.

### Import contract

Do not attempt to infer arbitrary colored/merged spreadsheet layouts from
screenshots. Provide a versioned Scribe import format with local preview:

- accounts and wallets;
- card settlement mappings;
- subscription register and effective prices;
- salary plan template;
- budget categories/targets;
- opening balances and wallet splits.

Preferred initial format is a local ZIP of UTF-8 CSV files plus a manifest JSON,
or individual CSV files. Add XLSX parsing only after a real exported sample is
provided and its mapping is tested; do not add a dependency speculatively.

### Schema

Implement revision `0016` only when needed:

- `import_batches`: format version, local filename hash, status, counts,
  sanitized error summary, preview/approval timestamps.
- `import_rows`: batch, entity kind, source row key, normalized payload, status,
  sanitized errors, created entity reference.

Do not store original spreadsheet files or arbitrary formulas in SQLite.

### Cutover

- Accepted cutover observation: 2026-08-14 23:10:28 Asia/Taipei, based on the
  final read-only Sheet snapshot used for the approved file.
- The six tracked account totals and signed splits are current observations;
  Wangdao is deliberately outside the tracked ledger.
- If a historical opening value is unavailable, stop and request a reconciled
  value; never invent it from today's balance.
- Pre-cutover Gmail transactions remain searchable/reportable but are not posted
  into the new ledger by default.
- Page 3 rows through the observation instant are immutable daily-history
  evidence. They are visible by date but excluded from posted totals, balances,
  reservations, and budget actuals because those values are already inside the
  current opening positions.
- Preview shows all normalized accounts, wallets, cards, subscription totals,
  salary destinations, opening splits, duplicates, and errors.
- Apply an approved batch atomically and idempotently.
- Google Sheets becomes read-only archive; export from Scribe is one-way.

### Backup/restore

**Delivered in B20** (`backup`, `backup-list`, `restore`); see 15K.

- Use SQLite online backup API or a safe WAL checkpoint plus verified copy.
- Backup includes database and a manifest of schema revision/app version; OAuth
  token/credentials remain separately protected and are not bundled by default.
- Restore is an explicit offline operation with integrity check and rollback
  copy. Never overwrite the active database without confirmation.
- Test restore into a temporary directory and compare integrity counts/balances.

### Performance and privacy hardening

- Measure daily queries, balance queries, monthly budget, and clearing with a
  synthetic large dataset.
- Inspect query plans and add only evidence-backed indexes.
- Run SQLite `integrity_check`, foreign-key check, and WAL diagnostics.
- Confirm log redaction and that imports/backups are gitignored.
- Document FileVault/unencrypted SQLite risk and secure deletion limitations.
- Provide a local-data clearing command with exact target preview and explicit
  confirmation; do not delete credentials unless separately selected.

### Acceptance tests

- Invalid preview writes no domain data.
- Applying the same batch twice is idempotent.
- Signed opening wallet splits equal each account balance; negative envelopes
  are preserved and zero shares do not create zero-value postings.
- Pre-cutover source transactions are not silently posted.
- Backup restores to the same migration revision, entry count, balances, and
  schedule execution keys.
- Large synthetic mailbox/ledger queries stay within reviewed local targets.
- No real user data appears in tests or commit history.

### Commit

Suggested message: `feat: add budget cutover and recovery tools`

Stop after B9 review.

### Applied B9 cutover design

Revision `0016_import_batches` adds `import_batches`, `import_rows`, and
`legacy_daily_activities`. Import rows and legacy daily rows are immutable at
the database layer; downgrade refuses to erase any history. The source file is
kept only under gitignored `data/imports/` with restrictive permissions. It
contains normalized values, last-four account hints, and stable source keys,
not formulas, cell formatting, or complete account numbers.

The current import includes tabs 1--5 except that tab 4 is reference-only and
tab 5 remains pending until card configuration exists. Tabs 6--9 are ignored.
Positive subscription prices seed the canonical register; zero-price rows are
not invented as active services. The monthly living target is TWD 20,000 with
the explicit TWD 3,000 buffer, while salary funding remains TWD 23,000. The
subscription salary line calculates from the B4 register instead of copying a
rounded total by hand.

The applied batch also becomes the enforceable opening boundary. Events whose
canonical occurrence or proposed journal effective time is at or before its
cutoff remain audit evidence but cannot create another primary entry. Review
and budget queries omit those events from actionable counts; events strictly
after the cutoff continue through the normal allocation workflow. This is
enforced in the shared journal service rather than trusted to a route filter,
and it does not mutate the historical event statuses.

## 15A. Budget Milestone B10 — Accounts sheet and wallet reallocation

Status: **implemented; awaiting review.** No schema change; Alembic head stays
at `0017_card_web_setup`.

### Goal

Replace the stacked account panels with the sheet the user actually kept by
hand, and let a wallet amount be edited in place without turning a split into
an adjustment.

### Layout

- One column per financial account; one row inside it per wallet; the column
  ends in its own total (ADR-B085).
- Columns keep their own row axis and are not aligned to each other.
- Wallets read in creation order rather than by name.
- The strip scrolls inside its own wrapper on a wide screen and stacks into
  full-width columns at 620px, so the page never scrolls sideways.
- The masked reference is displayed. A full account number is never stored or
  rendered, and the preferential-rate line from the user's spreadsheet is
  deliberately not modelled in this milestone.

### Editing

- A wallet cell is an input holding the current amount. Focusing it reveals a
  counterpart picker listing the other wallets of the same account, plus one
  apply control; nothing is submitted until the user applies it.
- `POST /money/wallets/{wallet_id}/balance` computes the signed difference
  between the submitted result and the rendered balance and posts one internal
  transfer in that direction (ADR-B086).
- The counterpart must belong to the same account; a cross-account counterpart
  is refused rather than silently moving money between banks.
- The rendered balance is submitted and re-checked, so a stale column is
  refused instead of posting against a total nobody read.
- Retyping the current number posts nothing at all.
- A negative envelope may be typed; B7 already keeps signed wallet balances.
- Money genuinely entering or leaving the bank keeps the existing bank-movement
  and transfer forms, which are the ones that ask for a date and a description.
- A card-liability column and a single-wallet column are read-only, because
  neither can express a split.

### Expected files

- `app/domain/errors.py`, `app/domain/budget_rules.py`
- `app/application/services/bank_movements.py`
- `app/infrastructure/database/spending_queries.py`
- `app/presentation/web/app.py`,
  `app/presentation/web/templates/money_accounts.html`,
  `app/presentation/web/static/styles.css`
- `tests/unit/test_ledger_rules.py`, `tests/web/test_spending_routes.py`

### Acceptance tests

- An edited cell moves money inside its account and leaves the total unchanged.
- The edit is recorded as a transfer, never as an adjustment, and is not
  counted as spending.
- A counterpart in another account is refused.
- A stale rendered balance is refused and nothing is posted.
- Retyping the same number posts nothing.
- Resubmitting one edit moves the money exactly once.
- A negative envelope can be typed.
- A single-wallet account offers no editable cell.
- The new route stays absent from the external read-only surface.

### B10 implementation record

`transfer` and the new `reallocate` now share one `_stage_wallet_move` helper,
so both hand-entered transfers and edited cells use the same idempotency and
replay path instead of two copies of it.

An edited cell carries no time of its own, so a first submission is stamped on
arrival. A retry of the same token would then never match the recorded entry's
effective time and would be rejected as a conflicting operation, so a replay
deliberately reuses the stored occurrence instead of stamping a second one.

The optimistic balance check runs only on a first submission. A replay is
re-proving the entry it already wrote, and its starting balance is by
definition the one that edit moved away from.

Wallet ordering changed from name to creation order in the shared account
query, so the account detail page inherits the same arrangement.

Known limitation: row order is wallet creation order, not an order the user
can rearrange. A user-defined ordering would need its own column, migration,
and reordering UI, which this milestone does not open.

### Commit

Suggested message: `feat: edit wallets in an accounts sheet`

Stop after B10 review.

## 15B. Budget Milestone B11 — Wallet arrangement

Status: **implemented; awaiting review.** Alembic head moves to
`0018_wallet_display_order`.

### Goal

Let each account's sheet column read in the order its owner arranged it in,
rather than in an order the database happened to produce.

### Schema

`0018_wallet_display_order` adds `wallets.display_order` with an index on
`(financial_account_id, display_order)`.

The backfill is the order the rows already read in, so the page is unchanged
the moment the revision lands. The one deliberate exception is the residual
`unallocated` envelope, which is seeded to the end of its column: it is what is
left over rather than a named envelope, and the arrangement being reproduced
has no row for it.

`display_order` is presentation only — no money, no identity, no idempotency
key — so unlike the history-bearing columns of earlier revisions its downgrade
simply drops it (ADR-B087).

### Behavior

- `GET /money/accounts/{account_id}/order` lists the account's wallets with the
  position each currently holds.
- `POST /money/accounts/{account_id}/order` stores the arrangement. Every
  wallet of the account must be named exactly once; a partial submission is
  refused rather than completed with a guess.
- Positions are a sort key, not a permutation: a repeated or skipped number is
  resolved against the order already on screen, and the stored values are
  renumbered from the result so gaps never accumulate.
- Arranging a column posts nothing and moves no money.
- A new wallet is appended to the end of its column rather than inserted into
  an existing arrangement.
- The sheet links to the page from any column holding more than one wallet.

### Expected files

- `app/infrastructure/database/migrations/versions/0018_wallet_display_order.py`
- `app/domain/errors.py`, `app/domain/budget_rules.py`,
  `app/domain/budget_models.py`, `app/domain/ports/budget_repositories.py`
- `app/application/services/ledger.py`
- `app/infrastructure/database/budget_models.py`,
  `app/infrastructure/database/budget_repositories.py`,
  `app/infrastructure/database/spending_queries.py`
- `app/presentation/web/app.py`,
  `app/presentation/web/templates/money_wallet_order.html`,
  `app/presentation/web/templates/money_accounts.html`,
  `app/presentation/web/static/styles.css`
- `tests/migrations/test_wallet_display_order_migration.py`,
  `tests/unit/test_ledger_rules.py`, `tests/web/test_spending_routes.py`

### Acceptance tests

- `0018` seeds creation order and sends the residual envelope last.
- `0018` round-trips, because an arrangement is presentation only.
- A saved arrangement reorders the sheet column.
- An arrangement moves no money and records no event.
- A repeated position falls back to the order already on screen.
- An arrangement that omits or invents a wallet is refused.
- A new wallet joins the end of its column.
- Both new routes stay absent from the external read-only surface.

### B11 implementation record

Reordering deliberately does not bump `Wallet.version`. An arrangement is
presentation, so it must not make a concurrent configuration edit of the same
wallet look like a conflict.

A refused downgrade past `0017` now leaves the database at `0018` rather than
`0017`: Alembic runs the whole downgrade path in one transaction, so the guard
in `0017` rolls back `0018`'s successful downgrade with it. The head assertions
in eight existing tests were updated to `0018` accordingly.

Known limitation: the residual-last rule is a property of the `0018` backfill,
not of wallet creation. A brand-new account still creates its `Unallocated`
wallet first, so that column starts with the residual at the top until its
owner arranges it.

### Commit

Suggested message: `feat: arrange the wallets of an account`

Stop after B11 review.

## 15C. Budget Milestone B12 — Account arrangement and a sheet that wraps

Status: **implemented; awaiting review.** Alembic head moves to
`0019_account_display_order`.

### Goal

Let the sheet's columns read in the order their owner arranged them, and stop
hiding columns behind a horizontal scrollbar.

### Schema

`0019_account_display_order` adds `financial_accounts.display_order` with an
index on it, backfilled from creation order. Presentation only, with the same
droppable downgrade as `0018` (ADR-B088).

### Behavior

- `GET`/`POST /money/accounts/order` arrange the account columns, using the
  same rule, form shape, and refusals as one column's wallets. The literal
  path is declared before `/money/accounts/{account_id}` so it is not captured
  as an account id.
- A new account is appended to the end of the sheet.
- The sheet is a wrapping grid rather than a horizontal strip, so no width
  requires a horizontal scrollbar and no column is ever off-screen
  (ADR-B089).
- The two arrangements report distinct confirmations rather than sharing one.

### Expected files

- `app/infrastructure/database/migrations/versions/0019_account_display_order.py`
- `app/domain/errors.py`, `app/domain/budget_rules.py`,
  `app/domain/budget_models.py`, `app/domain/ports/budget_repositories.py`
- `app/application/services/ledger.py`
- `app/infrastructure/database/budget_models.py`,
  `app/infrastructure/database/budget_repositories.py`,
  `app/infrastructure/database/spending_queries.py`
- `app/presentation/web/app.py`,
  `app/presentation/web/templates/money_account_order.html`,
  `app/presentation/web/templates/money_accounts.html`,
  `app/presentation/web/static/styles.css`
- `tests/migrations/test_account_display_order_migration.py`,
  `tests/unit/test_ledger_rules.py`, `tests/web/test_spending_routes.py`

### Acceptance tests

- `0019` seeds creation order rather than how a bank is spelled, and
  round-trips.
- A saved arrangement reorders the columns.
- An arrangement that omits an account is refused.
- A new account joins the end of the sheet.
- The sheet needs no horizontal scrollbar.
- Both new routes stay absent from the external read-only surface.

### B12 implementation record

`resolve_wallet_arrangement` became `resolve_display_arrangement` and
`WalletOrderingError` became `DisplayOrderingError`: one rule now serves both
sheet axes rather than being duplicated for accounts. The web layer translates
the shared `position_{id}` form shape in one helper, and which ids are legal is
decided by the application service against the axis they belong to.

Known limitation: arranging is retyped positions rather than dragging. A drag
interaction would need a JavaScript dependency the project does not carry.

### Commit

Suggested message: `feat: arrange the accounts of the sheet`

Stop after B12 review.

## 15D-1. Budget Milestone B12A — The accounts sheet on one screen

Status: **implemented; awaiting review.** Recorded after the fact: the change
shipped in `754f005` without its architecture note, and the CSS referenced an
ADR-B091 that did not yet exist.

### Goal

Show every account without scrolling, at a row height that is still readable.

### Behavior

- `/money/accounts` opts out of the 1180px reading column and uses the window
  width, so six columns fit one band instead of wrapping into two (ADR-B091).
- Columns share the width evenly through `auto-fit`; `auto-fill` left empty
  tracks and pushed the columns to the left.
- Sheet density: the page heading is compact, its actions sit in the heading
  row, and rows use a sheet line-height rather than the body's reading one.
- The phone breakpoint still collapses the sheet to a single column.

### Expected files

- `app/presentation/web/static/styles.css`,
  `app/presentation/web/templates/base.html`,
  `app/presentation/web/templates/money_accounts.html`
- `tests/web/test_spending_routes.py`

### Acceptance tests

- The page opts into the wide shell and the sheet declares `auto-fit`, with no
  horizontal scroll container.

### B12A implementation record

Measured rather than guessed: before the change the page was 1762px tall in a
779px viewport, with 361px consumed before the first row and a 49px row. After
it, one wallet row is 36px and every column shares an equal width.

Row height was deliberately raised again after the first pass — 27px fitted
one screen but read as cramped. At 36px the tallest column needs roughly a
910px viewport; that trade is stated rather than hidden.

### Commit

Committed as `754f005`.

## 15D. Budget Milestone B13 — Salary plan sheet

Status: **implemented; awaiting review.** No schema change; Alembic head stays
at `0019_account_display_order`.

### Goal

Make the monthly salary plan readable and correctable as the sheet its owner
already keeps, instead of one six-field form per destination.

### Behavior

- `/allocations/templates` renders the plan grouped by the destination
  account, in the arrangement the accounts sheet stores, with one amount field
  per fixed destination and a fixed-item total (ADR-B090).
- `POST /allocations/templates/{template_id}/amounts` applies the amounts one
  submission changed, under each item's own expected version plus the
  template's.
- Only rows whose amount actually moved are written, so one correction does
  not bump fifteen untouched versions.
- A calculated destination has no amount field and an amount submitted for one
  is refused.
- An amount for an item of another plan, or for a deactivated item, is
  refused.
- Structural editing — wallet, calculation mode, category, order — keeps its
  existing per-item form, now behind a disclosure.
- Both existing behaviors are unchanged: a stale version is a conflict, and a
  generated month stays frozen.

### Expected files

- `app/application/services/salary_allocation.py`
- `app/application/queries/salary.py`,
  `app/infrastructure/database/salary_queries.py`
- `app/presentation/web/app.py`,
  `app/presentation/web/templates/salary_templates.html`,
  `app/presentation/web/static/styles.css`
- `tests/web/test_salary_routes.py`

### Acceptance tests

- The sheet groups destinations by the bank they land in.
- One submission saves a changed amount.
- An unchanged amount does not bump a version.
- A stale amount is a conflict, not a silent overwrite.
- An amount for a calculated line is refused.
- The structural editor is still available.
- The route stays absent from the external read-only surface.

### B13 implementation record

Grouping is computed in the read model rather than in the page, because this
Jinja build's `groupby` filter has no `sort` parameter and would have re-sorted
the groups by the spelling of a bank's name — the exact ordering ADR-B088
rejected.

The plan total covers fixed destinations only. Including a calculated one
would state a monthly figure the generation would not honour.

Known limitation: the sheet is one column of amounts, not several months side
by side. Amounts that differ per future month — a planned one-off, or a
contribution that steps down in November — still have nowhere to live; that
needs its own per-month override table and is not opened here.

### Commit

Suggested message: `feat: edit the salary plan as a sheet`

Stop after B13 review.

## 15E. Budget Milestone B14 — Empty states that explain themselves

Status: **implemented; awaiting review.** No schema change; Alembic head stays
at `0019_account_display_order`.

### Goal

Stop reporting an empty page as a fact about the user's money when it is
really a queue waiting for them.

### Behavior

- `DashboardSnapshot` gains `unconfirmed_today_count`, counted over the same
  local day its totals use.
- The landing page's today panel, when it has no posted spending *and*
  something is queued, says so and links to the two queues that clear it:
  today's unconfirmed parsed transactions and the events awaiting allocation
  (ADR-B092).
- A day with nothing queued keeps its plain "nothing spent" message, so the
  explanation does not become decoration.
- A day that already shows spending is never annotated, even when other work
  is queued.
- `SalaryMonthView` gains the applied opening cutoff and reports whether the
  month precedes it. `/allocations/{yyyy-mm}` explains an empty month three
  ways: before the opening cutoff (needs nothing), no template chosen, or no
  salary receipt recorded yet.

### Expected files

- `app/application/queries/dashboard.py`,
  `app/infrastructure/database/dashboard_queries.py`
- `app/application/queries/consolidated.py`
- `app/application/queries/salary.py`,
  `app/infrastructure/database/salary_queries.py`
- `app/presentation/web/templates/money_home.html`,
  `app/presentation/web/templates/salary_allocations.html`,
  `app/presentation/web/static/styles.css`
- `tests/unit/test_consolidated_dashboard.py`,
  `tests/web/test_salary_routes.py`

### Acceptance tests

- An empty day with queued work reports both queue sizes.
- An empty day with nothing queued keeps the plain message.
- A day that posted something is never reported as waiting.
- A month with no template says to choose one.
- A month with a plan but no receipt says to record one.
- A month before the opening cutoff says it needs nothing, and a later month
  does not.

### B14 implementation record

The salary month query reads the applied import batch directly rather than
routing through the ledger service, because this is a read boundary and the
service's accessor is transactional.

Deliberately not done: neither queue is cleared, counted into a total, or
auto-confirmed. The pages explain; the user still decides.

Known limitation: the landing page counts today's unconfirmed transactions but
the allocation queue is not date-scoped, because an event awaiting allocation
blocks the day it belongs to rather than the day it was parsed.

### Commit

Suggested message: `fix: say what an empty money page is waiting for`

Stop after B14 review.

## 15F. Budget Milestone B15 — One money input convention

Status: **implemented; awaiting review.** No schema change; Alembic head stays
at `0019_account_display_order`.

### Goal

Stop asking for minor units in half the product. A field that decides money
must not be a hundredfold ambiguity.

### Behavior

- Every amount field takes the written amount and is parsed with
  `parse_display_amount`; each form carries the currency it is written in
  (ADR-B093).
- Converted: budget target and buffer, subscription prices, salary receipt,
  salary template fixed amounts, manual salary lines, clearing schedule and
  autopay amounts, commitment coverage shares, review allocation rows, and the
  parsed-transaction override.
- `parse_display_amount` gains `allow_zero`, used only for budget buffer,
  subscription price, and a manual salary line. Spending stays positive and
  zero never admits a negative.
- The review page shows one allocation row; the second and third stay
  available behind a disclosure instead of two permanently empty blocks.

### Expected files

- `app/domain/money.py`
- `app/presentation/web/app.py` and the budget, clearing, subscription,
  salary, review, and transaction-detail templates
- `tests/unit/test_display_amounts.py` and the web route tests

### Acceptance tests

- Zero is accepted where it is a real planning answer and refused as a
  spending amount; allowing zero never allows a negative.
- Every converted form round-trips: what the page renders is what it accepts.
- Existing conflict, idempotency, and validation behaviour is unchanged.

### B15 implementation record

Converting the budget form surfaced a real defect rather than a test problem:
`parse_display_amount` refused `0`, so the buffer field the form ships with a
default of zero could not be submitted at all once it stopped taking minor
units. `allow_zero` exists because of that, not for convenience.

`transactions.amount_minor` keeps its form name: it identifies which field an
override applies to, so renaming it would change stored override keys. Only
the value it carries changed.

The currency is read with a default rather than required, so a form with no
typed lines — a salary generation with no manual destinations — is not
rejected for omitting a currency it never needed.

### Commit

Suggested message: `fix: ask for money the way it is written`

Stop after B15 review.

## 15G. Budget Milestone B16 — Assets and card debt read apart

Status: **implemented; awaiting review.** No schema change; Alembic head stays
at `0019_account_display_order`.

### Goal

Stop listing what is owed among the accounts that hold money.

### Behavior

- `ConsolidatedMoneyOverview` gains `asset_accounts` and
  `card_liability_accounts`, split on account type rather than on a name.
- The landing page renders two sections: 帳戶與小錢包 for accounts that hold
  wallets, and 卡費負債 for card liabilities (ADR-B094).
- The liability section is omitted when there is none, so the page does not
  grow an empty band for a user who tracks no cards.
- Reserved and available stay on asset cards only; a liability card says it is
  unpaid card spending and is not available funds.

### Expected files

- `app/application/queries/consolidated.py`
- `app/presentation/web/templates/money_home.html`
- `tests/unit/test_consolidated_dashboard.py`

### Acceptance tests

- A card liability never appears among the accounts that hold money, and an
  asset account never appears among the liabilities.

### B16 implementation record

The split is derived from `holds_wallets` and the card-liability type, the
same predicates `currency_positions` already uses, so the two totals on the
page cannot disagree about what counts as an asset.

### Commit

Suggested message: `feat: read assets and card debt apart on the home page`

Stop after B16 review.

## 15H. Budget Milestone B17 — Local state readable without parsing prose

Status: **implemented; awaiting review.** No schema change; Alembic head stays
at `0019_account_display_order`.

### Goal

Let a script or an operating model read local state without regular
expressions over formatted text.

### Behavior

- `CLAUDE.md` records how to operate this repository from a prompt: the
  backup-rehearse-apply order, the launchd agent that `kill` cannot stop, the
  `cp` that silently drops the WAL, and the facts that are easy to get wrong
  (minor units, the opening cutoff, why a total can be zero).
- `money status --json` answers what an arriving operator needs in one read:
  revision, accounts with balances, cards and their readiness, and what is
  waiting.
- `app/presentation/cli/render.py` holds one encoder, so every `--json`
  document is spelled the same way: enums as their values, datetimes as ISO
  strings, `Decimal` as text rather than a float that would round it
  (ADR-B095).
- `doctor --json` and `money balances --json` emit the same values their text
  output shows.
- A new `schema` command describes mapped tables and columns, with `--table`
  for one of them, read from metadata so it works before any migration.
- Mutations keep prose only.

### Expected files

- `CLAUDE.md`
- `app/presentation/cli/render.py`, `app/presentation/cli/app.py`,
  `app/presentation/cli/money.py`
- `tests/test_cli.py`, `tests/integration/test_ledger_cli.py`

### Acceptance tests

- `schema` describes a table without a database present and refuses an unknown
  table name.
- `doctor --json` lists exactly the checks its text form printed.
- `balances --json` carries exact minor units, so no amount is recovered from
  a formatted string.
- `status --json` reports revision, accounts, cards, and the waiting count,
  and reading it twice changes nothing.

### B17 implementation record

Chosen from measured friction rather than taste. Operating this CLI through a
shell in one session produced eight wrong column-name guesses — `transaction_at`
read as `occurred_at`, `profile_email` as `email_address`, `due_at_utc` as
`due_at`, and so on — each costing a round trip. `schema` removes that class of
failure outright.

`CLAUDE.md` exists because the operating knowledge earned in one session
otherwise dies with it: the next model rediscovers the WAL trap and the
launchd agent by making the same mistakes. It records what cost time, not what
is already obvious from the code.

Deliberately not done: the command names are still inconsistent
(`intake-confirmed` is not guessable from `events-backfill`). Renaming them is
a breaking change to a surface the user drives by hand, so it needs its own
decision rather than being folded in here.

### Commit

Suggested message: `feat: read local state as JSON`

Stop after B17 review.

## 15I. Budget Milestone B18 — Every web write reachable from a command

Status: **implemented; awaiting review.** No schema change; Alembic head stays
at `0019_account_display_order`.

### Goal

Make the product fully operable without a browser, because it will be driven
by prompt.

### Behavior

- Fourteen new commands cover the writes that had none, including
  `transaction-confirm` and `allocate` — the daily loop (ADR-B096).
- Commands that post default to a preview and require `--apply`.
- `MoneyRuntime` gains the transaction-side services, so one engine and one
  lifecycle serve every command.
- `ControlBackgroundSync` takes an optional worker, so a cancel-only caller
  need not own one.
- `CLAUDE.md` documents the loop as commands.

### Expected files

- `app/bootstrap.py`, `app/application/services/sync_control.py`
- `app/presentation/cli/money.py`
- `CLAUDE.md`
- `tests/integration/test_ledger_cli.py`

### Acceptance tests

- Every `@application.post` route is named in the covered set, so a new web
  write without a command fails the suite by name.
- The posting commands all offer `--apply` and `--dry-run`.
- The daily loop runs end to end from commands: a preview names the mismatch
  between a line total and the event, an applied allocation posts, and an
  incomplete split is refused without posting.
- Arranging stores the sequence it was given and refuses a partial one; a card
  is registered incomplete and configured ready; a stale version is refused for
  a card, a category, and a wallet reallocation.

### B18 implementation record

Found by enumerating the web routes against the command list rather than by
recollection: fifteen writes had no command, and the two most important were
the ones the daily workflow is made of. Everything built earlier in this
sequence made the state *readable*; none of it made the product *operable*.

`/transactions/{id}/reparse` is covered by `email-reparse`: the route itself
resolves the transaction to its email and reparses that, so a second command
would be the same call with an extra lookup.

The first pass shipped these commands with only help-text tests, which is how
coverage of `money.py` sat at 69% while the suite was green: the gate measures
the whole project, so a poorly covered new file hides inside a large one.
Writing execution tests raised it to 76% and immediately paid for itself —
seeding the daily loop rediscovered, from the tests alone, that a card
purchase cannot post until its card is configured and that registering a card
never backfills events staged before it.

Fixed while testing: `money balances` ordered accounts by institution and
wallets by name, so it disagreed with the accounts sheet about the order of
the same rows. Both now read the stored arrangement (ADR-B087, ADR-B088).

Known limitation: command names are still inconsistent with each other
(`intake-confirmed` beside `transaction-confirm`). Renaming is a breaking
change to a surface the user also drives by hand, so it stays a separate
decision.

### Commit

Suggested message: `feat: make every web write reachable from a command`

Stop after B18 review.

## 15J. Budget Milestone B19 — Reads carry the token their writes demand

Status: **implemented; awaiting review.** No schema change; Alembic head stays
at `0019_account_display_order`.

### Goal

Finish covering the commands B18 added, and make the ones that need an
optimistic token actually callable.

### Behavior

- `event-show` and `transaction-show` report the version, `updated_at`, and
  `email_id` that `event-note`, `transaction-review`, and `email-reparse`
  require; `category-list` prints each category's version (ADR-B097).
- Both new reads take `--json`.
- `MoneyRuntime` gains the dashboard queries, so a transaction read uses the
  same engine as every other command instead of opening its own.

### Expected files

- `app/bootstrap.py`, `app/presentation/cli/money.py`
- `tests/integration/test_ledger_cli.py`

### Acceptance tests

- A transaction reports the token its review demands, and a stale token is
  refused.
- A review takes only the fields it named as overrides.
- A note is written and cleared under its version, changes no status, and a
  stale version is refused.
- A supplementary card is marked and unmarked.
- An email reparse reads retained text and an unknown email is refused.
- Cancelling a sync that is not running is refused rather than reported as
  accepted.

### B19 implementation record

Writing the tests is what exposed the gap: the commands could not be exercised
because nothing reported the tokens they require, which means no operator
could have used them either. Coverage of `money.py` went 76% to 79%, but the
number is incidental — what mattered was that five commands were unusable and
now are not.

### Commit

Suggested message: `feat: report the tokens a correction has to quote`

Stop after B19 review.

## 15K. Budget Milestone B20 — Backup and restore as product, not ritual

Status: **implemented; awaiting review.** No schema change; Alembic head stays
at `0019_account_display_order`.

### Goal

Deliver the B9 backup/restore contract, which was still unbuilt while the
discipline it describes was being followed by hand.

### Behavior

- `backup` writes through SQLite's online backup API, verifies the copy
  against the live database, and writes a manifest carrying the schema
  revision and application version (ADR-B098).
- `backup-list` reports what exists, newest first, with `--json`.
- `restore` previews by default, names the row counts that would change, and
  overwrites only with `--apply --confirm REPLACE`. It backs up the file it
  replaces first and refuses a backup that fails its own integrity checks.
- Backups and manifests are written `0600` under the gitignored data
  directory.

### Expected files

- `app/infrastructure/database/backup.py`, `app/presentation/cli/app.py`
- `tests/test_backup.py`, `tests/test_cli.py`

### Acceptance tests

- A backup is verified against the database it came from, and captures writes
  still sitting in the write-ahead log.
- A manifest records the revision and application version.
- Restoring brings back the state the backup held, and the replaced database
  survives as a rollback copy.
- A file that is not a database is refused, before anything is overwritten.
- `restore --apply` without the typed confirmation refuses.

### B20 implementation record

This existed only as a scratch script used throughout the previous session:
six backups, four rehearsals, and one real restore of the user's database were
performed with a tool the product did not ship. `CLAUDE.md` told the next
operator to use the backup API without giving them anything that did.

Two defects surfaced while testing rather than in use: `read_facts` let
SQLite's own `DatabaseError` escape instead of refusing, which would have
surfaced as a driver traceback in the middle of a restore; and two backups
inside one second collided, which is a real case because a rollback copy is
taken immediately before a restore.

Deliberately not done: retention. Deleting old backups is itself a risky
action and the user chose to keep it manual.

### Commit

Suggested message: `feat: back up and restore the local database`

Stop after B20 review.

## 15L. Budget Milestone B20A — Review fixes

Status: **implemented; awaiting re-review.** Applied after a review of the
preceding fifteen commits found three high-risk and two medium defects. All
five were confirmed in the code before anything was changed.

### Fixed

1. **A restore could race the running service.** It deleted the write-ahead
   log and overwrote in place, while the LaunchAgent still held the file. It
   now stages, verifies, and moves into place with an atomic replace; sidecars
   are removed only after the swap; and it refuses outright when the database
   still looks in use, with `--force` as the explicit override (ADR-B098).
2. **An unbalanced ledger overwrote the live database before being caught.**
   Balance, integrity, foreign keys, and the schema revision are now all
   checked before the live file is touched, and again on the staged copy. A
   backup of an unbalanced database no longer reports `verified=True`.
3. **The clearing page parsed every currency as the configured default.** The
   currency now comes from the funding wallet, the plan, or the frozen run,
   read on the server. The hidden `currency` input is gone, because the page
   could not honestly assert one (ADR-B099).
4. **The Web refused zero where the domain allows it.** A budget target, a
   plan-sheet amount, and an optional fixed amount accept zero again; the
   salary page defaults manual lines to zero, so regeneration returned 422.
5. **The CLI could not drive a wallet negative** while the Web could. Both now
   use `allow_signed`, matching ADR-B075.

### Acceptance tests

- An unbalanced backup is refused and the live database is byte-identical
  afterwards; an unsupported revision is refused; an open database is reported
  as in use.
- A JPY amount is never parsed with TWD precision, and the clearing form
  asserts no currency.
- A zero budget target, a zero plan-sheet amount, and a zero fixed item are
  accepted.
- The CLI drives a wallet negative exactly as the Web does.

### B20A implementation record

Verified against the live database with the service running: the restore was
refused, naming the un-checkpointed write-ahead log, and the database was
unchanged.

The reviewer was right that the earlier tests dodged the real case by calling
`engine.dispose()` first. The in-use check is now its own test, and the
unbalanced-restore test asserts the live facts are identical afterwards rather
than merely that an error was raised.

### Commit

Suggested message: `fix: make a restore prove itself before it replaces anything`

## 15M. Budget Milestone B20B — A restore that proves exclusion

Status: **implemented; awaiting re-review.** Applied after a second review
found two P1 defects and one P2 still open. All three were reproduced before
anything was changed.

### Fixed

1. **`holders()` could not detect a live process.** Reproduced exactly as
   reported: after `wal_checkpoint(TRUNCATE)`, both an idle connection and an
   active read transaction were invisible. Replaced by a POSIX advisory lock —
   shared for the serving process's lifetime, exclusive for a restore
   (ADR-B100). `holders()` is deleted rather than improved, because a function
   whose answer cannot be trusted should not be available. `--force` is
   removed with it.
2. **A failed restore deleted the live sidecars.** The `finally` block cleared
   `-wal` and `-shm` regardless of outcome, so a refusal could discard
   committed, un-checkpointed frames of the database still in place. Sidecars
   are now cleared only after `os.replace` has succeeded.
3. **A manual salary line still refused zero.** `allow_zero` had been added to
   `_commitment_lines`, not to `_manual_salary_amounts`; the earlier record
   claimed otherwise and was wrong. Both forms default that field to zero.

### Acceptance tests

- A restore is refused while a shared lock is held, and the live facts are
  identical afterwards.
- The same holds for a checkpointed database with an open connection — the
  case the heuristic missed entirely.
- A refused restore leaves the write-ahead log in place; a successful one
  clears the sidecars of the file it replaced.
- `restore --help` offers no `--force`.
- A manual salary line accepts zero.

### B20B implementation record

Verified against the live database with the service running and the
write-ahead log checkpointed to zero bytes — the exact state in which the old
heuristic would have allowed the overwrite. The restore was refused and the
database was unchanged.

The reviewer was right twice over: not only was the check unreliable, the
previous milestone's record asserted a fix for the manual-salary zero that had
never been made. Claims about what was fixed are worth no more than the tests
behind them.

### Commit

Suggested message: `fix: prove exclusion with a lock instead of guessing`

## 15N. Budget Milestone B20C — Every reader claims the database

Status: **implemented; awaiting re-review.** Applied after a third review
found the shared lock covered only the Web application.

### Fixed

The claim was taken in the FastAPI lifespan, so the generated three-hourly
scheduled-sync agent, `python -m app sync`, and every `money`
command opened the database without it. A restore could therefore take the
exclusive lock while a scheduled sync was mid-write, and the refusal message
only mentioned stopping the Web agent.

The claim now lives in `create_database`, the single point every reader passes
through, and is released on `engine_disposed` (ADR-B100). The Web application's
explicit lock is removed as a duplicate. The refusal names both LaunchAgents.

### Acceptance tests

- A second `create_database` in the same process blocks a restore, and the
  live facts are identical afterwards.
- Disposing an engine releases the claim, so a finished command does not block
  every later restore.

### B20C implementation record

Verified two ways against a real process rather than only in-process: a
background script holding an engine blocked a restore with no Web service
running at all, and with the service running the CLI, `doctor`, and `backup`
all continued to work — the lock is shared, so readers coexist and only a
restore excludes them.

One test failure was informative rather than noise: the `migrated_database`
fixture is itself a reader, so the release test had to dispose it too. That is
the behaviour, not a workaround.

Deliberately kept: `read_facts` uses a raw read-only `sqlite3` connection
rather than `create_database`, so inspecting a backup never claims the live
database.

### Commit

Suggested message: `fix: claim the database wherever it is opened`

## 15O. Budget Milestone B20D — Migrations claim the database too

Status: **implemented; awaiting re-review.** Applied after a fourth review
found Alembic still bypassing the advisory lock.

### Fixed

`create_database` covers every reader except one: Alembic builds its own
engine inside `env.py`. Reproduced exactly as reported — holding the exclusive
lock, `upgrade_database` moved the schema from `0018` to `0019` and only then
raised `MigrationError` from its status check. A restore running concurrently
could therefore have left a file that was half replaced and half migrated.

`run_migrations_online` now takes the shared claim around the migration, which
covers `python -m app init`, `upgrade_database`, and a direct Alembic
invocation with an explicit target (ADR-B100).

### Acceptance tests

- A migration cannot write while a restore holds the database, and the
  revision is unchanged afterwards.
- A migration runs normally when nothing holds it.

### B20D implementation record

The same script that reproduced the defect now reports `before: 0018` /
`after: 0018`, and the whole migration suite exercises the new boundary
because every one of its upgrades passes through it.

Verified against the live service as well: `init` and `doctor` continue to
work while the Web process holds its shared claim — shared locks coexist, and
only an exclusive one is excluded.

### Commit

Suggested message: `fix: claim the database while migrating it`

## 16. Test organization

Add tests alongside actual milestones; do not create empty test modules early.

```text
tests/
├── unit/
│   ├── test_ledger_rules.py
│   ├── test_event_matching.py
│   ├── test_budget_close.py
│   ├── test_subscription_requirements.py
│   ├── test_salary_allocation.py
│   └── test_schedule_state_machine.py
├── integration/
│   ├── test_financial_event_posting.py
│   ├── test_manual_gmail_linking.py
│   ├── test_clearing_execution.py
│   ├── test_reconciliation.py
│   └── test_budget_web_routes.py
├── repositories/
│   └── test_budget_repositories.py
└── migrations/
    └── test_budget_migrations.py
```

All fixtures are synthetic and anonymized. Use explicit fake bank/card labels
or the institution names without actual account/card/balance data.

## 17. Reviewer verification after another model implements a milestone

The reviewer should not trust the implementation summary. Verify evidence:

1. Inspect commit scope and `git status`.
2. Compare delivered files and behavior with the exact milestone above.
3. Run full quality gate and migration upgrade on a new temporary database.
4. Upgrade a temporary copy of the pre-budget schema and compare row counts.
5. Review every money write for transaction boundary and idempotency.
6. Confirm no route or service performs direct SQL/Gmail calls.
7. Run invariant diagnostics:
   - no unbalanced journal entry per currency;
   - every physical asset posting has the correct wallet;
   - wallet totals equal physical-account ledger totals;
   - at most one confirmed event link per parsed transaction;
   - schedule execution key is unique;
   - no posted entry was updated in place.
8. Inspect logs for sensitive fields.
9. Test local offline startup.
10. Test external Host denial for every new sensitive route.
11. For UI milestones, inspect desktop and mobile behavior and compare with the
    approved Prototype rather than relying only on route tests.
12. Report deviations by severity and do not approve the next milestone until
    required fixes are committed.

## 18. Implementation handoff response template

Every implementation agent should finish a milestone with:

```text
Milestone:
Outcome:
Design decisions applied:
Files changed:
Migration and backup evidence:
Ruff result:
Ruff format result:
mypy result:
pytest result and coverage:
Manual/visual checks:
Security and privacy review:
Known limitations within this milestone:
Commit hash:
Ready for reviewer: yes/no
```

The words “tests pass” are invalid without actual command output. The agent must
stop after this handoff and wait for review.

## 19. Remaining setup data, not architecture decisions

These values are intentionally configured during setup/import rather than
hard-coded:

- exact financial account display names and masked references;
- opening balances and wallet splits at cutover;
- the destination wallet for unused monthly budget;
- card last four, holder role, settlement account, and default funding wallet;
- schedule amounts/dates for each bill and autopay;
- subscription names, effective prices, and cadence;
- salary template amounts;
- normalized import mapping for an actual exported spreadsheet sample.

Missing setup values do not authorize an implementation agent to infer personal
financial data from screenshots.
