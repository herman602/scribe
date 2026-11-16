# ADR-016: Encrypted-original attachment retention

- Status: Accepted
- Date: 2026-07-27

## Decision

During parser development, retain the original encrypted PDF and extracted text.
Never persist decrypted PDF bytes. Offer extracted-text-only and purge policies.

## Consequences

Reparsing is possible without Gmail while local sensitive storage grows. Backup,
purge and full-disk-encryption guidance must cover encrypted originals and
extracted plaintext.
