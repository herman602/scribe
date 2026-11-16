# Architecture Decision Records

Architecture decisions are immutable once accepted. A changed decision receives
a new ADR that supersedes the old record.

| ADR | Decision | Status |
|---|---|---|
| [001](001-modular-monolith.md) | Pragmatic Clean Architecture modular monolith | Accepted |
| [002](002-account-aware-schema.md) | Account-aware schema, single-account UX | Accepted |
| [003](003-sqlite-wal.md) | SQLite WAL and durable connection settings | Accepted |
| [004](004-transaction-overrides.md) | Parsed, manual and effective transaction values | Accepted |
| [005](005-parser-source-key.md) | Stable parser source keys and superseding | Accepted |
| [006](006-sender-filtered-sync.md) | Sender-filtered Gmail synchronization | Accepted |
| [007](007-body-retention.md) | Plain-text body retention policies | Accepted |
| [008](008-gmail-readonly.md) | Gmail read-only OAuth scope | Accepted |
| [009](009-local-worker.md) | SQLite-leased local worker process | Accepted |
| [010](010-no-default-llm.md) | No default external LLM | Accepted |
| [011](011-utc-time.md) | UTC normalization with source-time evidence | Accepted |
| [012](012-local-web-assets.md) | Locally served pinned web assets | Accepted |
| [013](013-sender-rules.md) | Mutable institution sender rules | Accepted |
| [014](014-encrypted-pdf.md) | Local encrypted text-PDF extraction | Accepted |
| [015](015-pdf-secret-store.md) | OS credential store for PDF passwords | Accepted |
| [016](016-attachment-retention.md) | Encrypted-original attachment retention | Accepted |
