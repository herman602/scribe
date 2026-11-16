# ADR-009: SQLite-leased local worker process

- Status: Accepted
- Date: 2026-07-27

## Decision

Long-running sync/reparse jobs use a separate local process and durable SQLite
lease. Packaged normal `serve` mode supervises one worker; development reload
does not auto-spawn it.

## Consequences

Jobs survive HTTP request completion and are crash-isolated without Redis.
Lease recovery and subprocess lifecycle require explicit tests.
