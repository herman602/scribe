# Architecture

Scribe is a modular, local-first Python application. Gmail is an ingestion
adapter, SQLite is the durable local system of record, and the server-rendered
dashboard is one presentation adapter over application services. The accounting
core remains usable when Gmail is offline.

## Dependency direction

```text
presentation (Typer / FastAPI / Jinja2)
                 |
                 v
application (commands, queries, orchestration)
                 |
                 v
domain (records, rules, ports, errors)
                 ^
                 |
infrastructure (SQLite, Gmail, OAuth, PDF, keyring, launchd)
```

Domain modules do not import FastAPI, Typer, SQLAlchemy, Google clients, or
launchd. Application services depend on narrow ports. `app/bootstrap.py` is the
composition root that chooses concrete adapters. Routes and CLI commands parse
input, invoke a use case, and render output; they do not issue SQL or Gmail API
calls directly.

## Module map

- `app/domain`: immutable records, enums, money/scheduling/parser rules, ports,
  and stable user-facing error taxonomy.
- `app/application/services`: transactional use cases for sync, review,
  accounting, budgets, subscriptions, salary allocation, clearing, daily
  activity, source matching, and reconciliation.
- `app/application/queries`: read models optimized for dashboard and CLI views.
- `app/infrastructure/database`: SQLAlchemy models/repositories, Unit of Work,
  SQLite engine policy, locking, backups, and Alembic migrations.
- `app/infrastructure/gmail`: OAuth, token storage, bounded Gmail API gateway,
  MIME decoding, pagination, history, and retry.
- `app/infrastructure/parsers`: deterministic classification pipeline, generic
  parser, and format-specific bank parsers.
- `app/infrastructure/attachments`: local PDF extraction and OS credential-store
  integration.
- `app/infrastructure/macos`: generated current-user LaunchAgent adapter.
- `app/presentation`: Typer CLI and FastAPI/Jinja2/HTMX web UI.
- `app/worker`: in-process background sync and scheduled-transfer maintenance;
  durable ownership remains in SQLite leases.

## Gmail synchronization

Initial synchronization paginates `messages.list`, expands every relevant
thread through metadata, queues every underlying Gmail message ID, and fetches
messages individually. Discovery/page progress and per-message outcomes are
committed so interruption resumes without rereading the whole scope.

Incremental synchronization starts from the committed `historyId`, processes
message additions, deletions, and label changes with pagination, and advances
the cursor only after every item on a page is safely handled. A rejected old
cursor triggers sender-scoped reconciliation and a new baseline; it never
deletes the database.

Sender rules are mutable database configuration. They narrow full-sync Gmail
queries and gate incremental bodies after a metadata-only fetch. Message ID is
the unique email identity; thread ID is grouping metadata, never a substitute.

## Parsing and source evidence

MIME decoding produces bounded plain text, HTML-derived text, headers, and
attachment metadata. PDF extraction is local and bounded. The classification
pipeline combines sender recognition, keyword/amount/card/time/merchant
evidence, then tries a bank-specific parser and conservative generic parser.

Every stage returns decision, confidence, reason, extracted fields, parser name,
and version. Email, parser runs, classification stages, parsed transactions,
manual overrides, and canonical financial-event source links remain separate.
A parser rerun can refresh unposted derived data but cannot silently replace a
manual decision or posted journal.

## Accounting and planning

The journal is immutable and double-entry. Postings use signed integer minor
units, balance per currency, and reference explicit ledger accounts and optional
wallet/category identities. Reversal is the only correction to a posted entry.
Journal idempotency compares the complete economic payload, not just a key.

Canonical financial events bridge observed or manual activity to allocation.
Card spending increases liability and creates a funding commitment without
moving a bank balance. Cash/bank spending reduces the chosen asset wallet.
Refunds return to original category shares and release outstanding reservations
once. Manual decisions and source discrepancies remain auditable.

Budget periods, targets, buffers, subscriptions, salary generations, scheduled
clearing, daily views, balance snapshots, and reconciliation build on the same
journal and wallet identities. Scheduled transfers are policy assumptions until
reconciliation; they are not bank API instructions.

## Transactions and concurrency

SQLAlchemy repositories participate in an explicit Unit of Work. A use case
that creates allocations, commitments, a journal entry, and a state transition
commits them together or rolls all of them back. Optimistic versions protect
editable configuration. Immutable rows are additionally guarded by database
constraints/triggers where SQLite can express the invariant.

Cross-process sync and maintenance ownership uses SQLite leases. Database
backup is online; destructive restore requires an exclusive advisory lock held
against every Scribe engine. Migrations run transactionally under the documented
SQLite foreign-key protocol and perform a full foreign-key check before commit.
Published revisions are never edited.

## Web and security boundary

The dashboard is server-side rendered with local HTMX/Chart.js assets and no
Node build. It starts without Gmail and reads retained data from SQLite. The
default network surface is loopback. Trusted-LAN and Cloudflare Access surfaces
are explicit settings validated by host, route, CSRF, and identity policy.
Money administration remains local-only.

The public source tree excludes operational configuration, OAuth credentials,
tokens, databases, backups, logs, and generated services. CI and release
artifacts run the same deterministic scanner before publication.

## Extensibility rules

- Add a bank parser as an infrastructure adapter plus synthetic fixtures; do
  not add bank-specific branches to routes or repositories.
- Add a new source through a port and canonical event bridge; do not let it
  write the journal directly.
- Add a new posting shape inside the journal/application invariant boundary and
  a migration registry entry; never bypass `JournalService`.
- Keep currencies explicit and amount types integer-only.
- Keep optional external integrations disabled until a user explicitly opts in.
- Prefer a vertical slice with real tests over unused interfaces or placeholder
  abstractions.

Detailed historical accounting ADRs remain in `BUDGET_ARCHITECTURE.md`; the
implementation/review record remains in `BUDGET_IMPLEMENTATION_PLAN.md`.
