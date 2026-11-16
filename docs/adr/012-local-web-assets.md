# ADR-012: Locally served pinned web assets

- Status: Accepted
- Date: 2026-07-27

## Decision

Serve CSS, HTMX and Chart.js from pinned application assets rather than runtime
CDNs.

## Consequences

The dashboard remains offline-capable and avoids third-party requests. Package
size and dependency update responsibility increase slightly.
