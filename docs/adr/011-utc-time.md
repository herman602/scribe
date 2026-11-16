# ADR-011: UTC normalization with source-time evidence

- Status: Accepted
- Date: 2026-07-27

## Decision

Store normalized UTC timestamps while retaining exact Gmail internal
milliseconds and parser evidence for original text, timezone and assumptions.

## Consequences

Sorting and reporting are consistent. Parsers must never silently replace an
unknown transaction date or timezone with ingestion time.
