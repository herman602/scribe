# ADR-007: Plain-text body retention policies

- Status: Accepted
- Date: 2026-07-27

## Decision

Store decoded plain text by default, not raw HTML. Support full text,
summary-only, and purge-after-parse policies.

## Consequences

Parser development remains practical and raw-HTML rendering risk is reduced.
Purged content requires a Gmail refetch and cannot be reconstructed offline.
