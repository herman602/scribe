# Gmail OAuth setup and recovery

Every Scribe installation uses a user-owned Google Cloud project and Desktop
OAuth client. This repository does not ship, share, or offer a maintainer OAuth
client. Your `credentials.json` and token stay on your machine and must never be
attached to an issue.

## Create the Desktop client

1. Create or select a Google Cloud project.
2. Enable the Gmail API.
3. Configure Google Auth Platform Branding and Audience for the people who may
   authorize this local installation.
4. Add the Gmail scope under Data Access.
5. Create a Client with application type **Desktop app**.
6. Download its JSON and save it as `credentials.json` in the Scribe checkout.
7. Restrict it to your macOS user: `chmod 600 credentials.json`.
8. Run `uv run python -m app auth`.

Scribe opens the system browser, listens temporarily on `127.0.0.1` for the
callback, validates the mailbox with `users.getProfile`, and writes the token
atomically with private file permissions.

## Exact scopes

The default and recommended scope is exactly:

```text
https://www.googleapis.com/auth/gmail.readonly
```

If and only if processed-message labeling is enabled, Scribe requests exactly:

```text
https://www.googleapis.com/auth/gmail.modify
```

`gmail.modify` includes read access and permits Scribe to apply the configured
user label. Processed-message labeling is opt-in; changing the setting requires
reauthorization. Scribe does not request Gmail send, delete, or broad Google
Drive scopes.

## Testing versus Production

Google OAuth projects in Testing can issue refresh tokens that expire after
seven days for Gmail scopes. Keep `gmail_oauth_testing_mode = true` while the
project is in Testing so the dashboard can warn before the expected expiry.
After publishing the project to Production, set it to `false`; Production does
not require a six-day reauthorization cycle merely because Scribe is running.

## Expired or revoked authorization

When refresh fails, Scribe persists `reauthorization_required` even if no sync
run was created. Every dashboard page shows the warning. Recovery is explicit:

```bash
uv run python -m app auth
uv run python -m app doctor
uv run python -m app sync
```

`auth` requests consent again and replaces the local token only after a valid
profile is returned. Do not hand-edit `data/token.json`. Revoking Scribe in your
Google Account, deleting the Desktop client, changing scopes, or changing the
authorized account all require a new authorization.

## History reconciliation

Gmail may reject an old `startHistoryId` with HTTP 404. Scribe does not delete
SQLite in response. It performs a complete reconciliation inside the current
sender/query scope, updates additions/deletions/labels message by message, and
establishes a new baseline only after all work is committed.

## Processed-message label

After setting `gmail_labeling_enabled = true` and reauthorizing:

```bash
uv run python -m app label-processed
```

The backfill uses message IDs already in SQLite and does not download the
mailbox again. Failed label writes remain in a durable outbox for retry, and
Scribe ignores history events caused solely by its own label to avoid a loop.
