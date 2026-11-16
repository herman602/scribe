# ADR-008: Gmail read-only OAuth scope

- Status: Accepted
- Date: 2026-07-27

## Decision

Request only `https://www.googleapis.com/auth/gmail.readonly`. OAuth tokens stay
in a protected local token file, never SQLite.

## Consequences

Scribe cannot create labels, filters, send mail or modify Gmail state. Users must
create an optional optimization label/filter themselves.
