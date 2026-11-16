# ADR-005: Stable parser source keys and superseding

- Status: Accepted
- Date: 2026-07-27

## Decision

Every parser candidate has a stable key within its source email. Unmatched old
candidates become superseded instead of being hard-deleted.

## Consequences

Reparsing preserves audit and manual overrides. Ambiguous matching favors
duplicate records for review over destructive overwrite.
