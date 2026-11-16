# Privacy and local security

Scribe processes email and financial records. Local-first means the application
does not require Scribe-operated infrastructure; it does not mean the machine,
SQLite file, or backups are encrypted automatically.

## Local data inventory

- `credentials.json` is the user-owned Google Desktop OAuth client definition.
- `data/token.json` contains Gmail access and refresh credentials. It is written
  atomically with private permissions and exact-scope validation.
- `config.toml` and `.env` contain local settings. They should not contain OAuth
  tokens or PDF passwords, but access settings may still identify a deployment.
- `data/app.sqlite3`, WAL, and SHM files contain synchronized email metadata,
  retained body/PDF text according to policy, parser evidence, transaction
  review state, accounts, wallets, journal entries, budgets, subscriptions,
  salary plans, scheduled movements, notes, and reconciliation snapshots.
- `data/logs/` contains redacted operational logs and generated-service output.
- `data/backups/` contains verified copies of the same sensitive database.
- PDF passwords live in the operating-system credential store. SQLite keeps
  only opaque references.

All of these paths are excluded from Git. The public-tree scanner rejects them
if they are accidentally tracked. Enable FileVault or equivalent full-disk
encryption, protect backups separately, and keep the local user account secure.

## Network and browser boundary

The default listener is exactly `127.0.0.1:8000`. Host validation is enabled,
CORS is not enabled, state-changing forms use SameSite/HttpOnly double-submit
CSRF cookies and exact Origin checks, and stored email HTML is never executed.
HTMX, Chart.js, CSS, and other normal dashboard assets are served locally; the
browser does not contact a CDN or analytics endpoint.

Trusted-LAN mode is an explicit opt-in that binds `0.0.0.0` while accepting
only configured exact private IP Hosts. It adds no login and uses plain HTTP.
Every device that can reach that private address can read and modify the full
ledger, so use it only on a controlled home network. Never enable router port
forwarding or a subnet/wildcard allowlist.

The optional Cloudflare mode accepts one exact hostname. It exposes only the
configured narrow report surface; money administration, source email, sync,
parser actions, and most writes remain local-only. The one optional confirmation
write independently verifies Cloudflare Access JWT issuer, audience, expiry,
and exact email plus HTTPS same-origin CSRF. The tunnel must still be protected
by a single-user Access policy. A hostname or spoofed Host header is not
authentication.

## Gmail and OAuth

The default OAuth scope is exactly `gmail.readonly`. Enabling processed-message
labeling changes the exact requested scope to `gmail.modify`; it is never
enabled implicitly. Scribe does not send mail or request Google Drive access.

Full sync is sender-filtered by default. `--all-senders` is an explicit action
that can persist every message in the selected query scope. Thread discovery is
expanded and each Gmail message is processed independently. Incremental sync
uses mailbox history and performs a scoped reconciliation rather than deleting
SQLite if the history cursor expires.

OAuth refresh failures are persisted as a reauthorization warning without
logging tokens. Tests use fake flows and synthetic accounts; normal tests never
open a browser or contact Gmail.

## Email, attachments, and parsers

Body and attachment sizes, PDF pages, extracted characters, password attempts,
and extraction time are bounded. Plain or decrypted PDF bytes are memory-only
and never persisted. For encrypted text PDFs, the encrypted original and
bounded extracted text may be retained locally to support reparse.

The logger redacts common credential fields, bearer tokens, and long financial
number patterns. Application code must never log complete bodies, extracted PDF
text, OAuth tokens, passwords, balances, merchants, wallet/account names, or
free-form financial notes. Parser and money fixtures use invented data only.

Scribe has no external LLM fallback in R1. Classification and extraction are
deterministic local Python code. A future external processor would require a
separate opt-in privacy design; it must not be added behind a silent fallback.

## Financial records

Money uses signed integer minor units and explicit currencies; no float or
cross-currency aggregate is allowed. Account balances are derived from immutable
double-entry postings. Corrections create reversals rather than editing history.
Full account/card numbers are rejected; cards store only a four-digit suffix.

Manual edits, source links, allocation decisions, scheduled-transfer evidence,
salary generations, and balance snapshots remain auditable. Parser reruns do
not silently overwrite manual values or posted ledger history. Reconciliation
adjustments record a reason and snapshot relationship.

All money routes are local-only unless a route is explicitly included in the
narrow external report policy and protected by its independent tests. Bank and
card schedules are local assumptions, not confirmation from a bank.

## Backups, restore, and services

The backup command uses SQLite's online backup API and verifies revision,
integrity, foreign keys, journal balance, and protected row counts. Restore is
preview-only by default, requires explicit `--apply --confirm REPLACE`, demands
exclusive database access, and keeps a rollback copy.

Generated macOS LaunchAgents run as the logged-in user, never root. Their plist
files contain paths and commands but no Gmail/PDF/Cloudflare secrets. Service
stdout and stderr remain local. The machine must be awake and the user logged in
for web or scheduled sync work.

## Public issue and contribution hygiene

Never attach a real database, backup, token, credentials file, log, email, PDF,
statement, screenshot with balances, full account/card number, or password to a
public issue. Parser reports must use a hand-written or programmatically
generated minimal fixture. Follow `SECURITY.md` for private vulnerability
reports and `CONTRIBUTING.md` for the synthetic-data policy.

Security and privacy reports must state data-access failures honestly. A green
test suite proves local contracts, not that a live mailbox, bank format, tunnel,
or OAuth project is currently healthy.
