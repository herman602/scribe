# ADR-013: Mutable institution sender rules

- Status: Accepted
- Date: 2026-07-27

## Decision

Model multiple exact-address or opt-in domain rules per institution for 富邦、
陽信、國泰、第一、永豐、華南. Do not hard-code actual sender addresses.

## Consequences

The user can add, disable and backfill rules without a release. Exact addresses
are preferred; sender matching remains a signal, not proof of a transaction.
