# ADR-004: Parsed, manual and effective transaction values

- Status: Accepted
- Date: 2026-07-27

## Decision

Persist latest parsed fields, a schema-versioned manual override map, and indexed
effective fields. Parser reruns update effective fields only when the field has
not been manually overridden.

## Consequences

Querying remains efficient and provenance remains visible. A single transaction
service must enforce consistency between the three representations.
