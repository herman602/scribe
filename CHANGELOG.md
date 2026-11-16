# Changelog

All notable user-visible changes to Scribe will be documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
for public releases.

## Unreleased

No changes yet.

## 0.1.0 - 2026-11-16

The first technical self-hosted release.

### Added

- Local-first Gmail full and incremental synchronization with pagination,
  checkpoints, retries, thread message expansion, history reconciliation, and
  flexible sender rules.
- Deterministic transaction classification and parsers for documented Taiwanese
  bank notification shapes, including encrypted text PDFs and manual review.
- SQLite double-entry ledger, financial accounts, wallets, cards, budgets,
  subscriptions, salary allocation, clearing schedules, daily activity,
  reconciliation, notes, reversals, and audit evidence.
- FastAPI/Jinja2/HTMX dashboard with local, LAN, and explicitly protected
  external access modes; the default listener remains loopback-only.
- Verified online backup and guarded restore commands.
- Generated current-user macOS services for the web process and three-hourly
  synchronization schedule.
- Bring-your-own Google Desktop OAuth onboarding and authorization health
  warnings; no shared OAuth client or hosted backend.
- Public self-hosting, privacy, recovery, architecture, and bank-parser
  contribution documentation, including a constrained prompt for LLM-assisted
  parser work.
- Hermetic GitHub quality and secret-history workflows plus wheel/sdist privacy
  verification and installed-wheel smoke tests.

### Security and privacy

- No telemetry, hosted data path, or external LLM fallback is enabled.
- Credentials, tokens, SQLite data, backups, logs, PDF passwords, and generated
  service files remain local and are excluded from source and distributions.
- Browser assets are vendored so an offline dashboard does not fetch JavaScript
  from a CDN.
- External financial routes remain allowlisted and read-only/confirmation
  behavior is explicit; public hosting is not a supported deployment model.

### Known limitations

- R1 is for technical self-hosters comfortable with Python, `uv`, a terminal,
  and their own Google Cloud OAuth configuration.
- There is no one-click installer, desktop application, hosted service, or
  automatic updater. Background-service generation is macOS-only.
- Bank support refers to the exact notification formats documented in the
  parser guide, not a bank API or a guarantee against future format changes.
- Gmail authorization and live mailbox behavior cannot be proven by public CI;
  each user must complete and validate their own OAuth flow.
