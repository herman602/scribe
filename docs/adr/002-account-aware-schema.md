# ADR-002: Account-aware schema with single-account UX

- Status: Accepted
- Date: 2026-07-27

## Decision

The first release exposes one Gmail account, but Gmail message identity and sync
state include an internal account ID.

## Consequences

The schema carries a small additional foreign-key cost and avoids unsafe global
assumptions about Gmail message IDs if multi-account support is added later.
