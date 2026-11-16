# ADR-003: SQLite WAL and durable connection settings

- Status: Accepted
- Date: 2026-07-27

## Decision

Use SQLite on a local filesystem with foreign keys enabled, WAL journal mode,
`synchronous=FULL`, and a bounded busy timeout.

## Consequences

Dashboard reads can continue during short worker writes. Backup must include an
SQLite backup/checkpoint workflow, and network filesystem use is unsupported.
