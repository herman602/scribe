# ADR-015: OS credential store for PDF passwords

- Status: Accepted
- Date: 2026-07-27

## Decision

Persist PDF passwords only through an operating-system credential-store adapter.
SQLite and configuration store opaque references. Foreground no-echo prompt is
the non-persistent fallback.

## Consequences

Background extraction pauses when the credential store is locked. Scribe never
weakens storage by silently creating a plaintext password file.
