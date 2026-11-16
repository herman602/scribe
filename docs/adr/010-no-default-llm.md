# ADR-010: No default external LLM

- Status: Accepted
- Date: 2026-07-27

## Decision

Ship deterministic local parsers without an external LLM provider. A future LLM
adapter requires explicit installation, per-run consent and a separate privacy
decision.

## Consequences

Email content remains local and parsing is reproducible. Unrecognized formats
remain uncertain until a local parser is added or the user opts into a future
adapter.
