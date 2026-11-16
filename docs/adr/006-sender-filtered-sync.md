# ADR-006: Sender-filtered Gmail synchronization

- Status: Accepted
- Date: 2026-07-27

## Decision

Compile enabled sender rules into the initial Gmail query. Incremental sync uses
mailbox history, metadata-only sender gating, then full fetch for matches.
Expired history reconciles the complete enabled-sender scope.

## Consequences

Ordinary message bodies and attachments are not downloaded or stored. The local
process still transiently reads IDs and selected headers unless the user
configures a dedicated Gmail label.
