# Installation and first run

Scribe R1 is a self-hosted Python application for people willing to operate
their own local data and Google OAuth project. It is not a hosted account and
does not include a desktop installer.

## Prerequisites

- Python 3.12 or newer
- [uv](https://docs.astral.sh/uv/)
- Git
- a macOS, Linux, or other Unix-like machine for manual use; generated
  background services are currently macOS-only
- a Google account and a user-owned Gmail API Desktop OAuth client

## Install

```bash
git clone https://github.com/herman602/scribe.git
cd scribe
uv sync --extra dev
uv run python -m app init
```

`init` creates private `config.toml` and `data/` paths if absent, then migrates
the local SQLite database. It does not overwrite existing configuration.

Follow [Gmail OAuth setup](gmail-oauth.md), save the downloaded Desktop client
as `credentials.json`, then authorize and check the environment:

```bash
uv run python -m app auth
uv run python -m app doctor
```

`doctor` intentionally reports Gmail failures until credentials and a valid
token exist. It does not prevent the offline dashboard from starting.

## Configure the first synchronization

Add exact sender rules before a sender-filtered sync. The command below is an
example address only:

```bash
uv run python -m app sender-rules add \
  --institution-key example-bank \
  --institution-name "Example Bank" \
  --address notifications@example.com
```

Preview available commands with `--help`. Run a bounded first import:

```bash
uv run python -m app sync --full --query "newer_than:1y"
```

The sync saves page and message checkpoints. If interrupted, rerun the same
scope to continue. Scribe lists Gmail threads only as a discovery aid: it
expands each matching thread and fetches every underlying message separately.
Later `sync` calls use mailbox history incrementally.

## Start the dashboard

```bash
uv run python -m app serve
```

Open <http://127.0.0.1:8000>. Keep the default loopback listener until you have
reviewed [configuration and access modes](configuration.md). The application
can browse synchronized data without Gmail connectivity.

On macOS, optional generated user services are documented in
[macos-service.md](macos-service.md). Do not copy another user's plist or edit
an absolute path by hand.

## Upgrading

1. Create and verify a backup using [backup and restore](backup-and-restore.md).
2. Stop generated services if an upgrade needs an exclusive operation.
3. Pull the reviewed source update and run `uv sync --extra dev`.
4. Run `uv run python -m app init` to apply migrations.
5. Run `uv run python -m app doctor` and restart services.

Never point Alembic at an implicit database. Development migration commands
must specify an isolated target as documented in `DEVELOPMENT.md`.

## Troubleshooting

- OAuth refresh rejected: follow the recovery section in
  [gmail-oauth.md](gmail-oauth.md).
- Incremental history expired: rerun normal `sync`; Scribe performs a scoped
  reconciliation and preserves the existing database.
- Encrypted PDF needs a password: store it through the sender-rule password
  command. The password goes to the OS credential store, not SQLite.
- Restore says the database is in use: stop generated services and allow any
  foreground command to finish; never delete a lock file to bypass the guard.
- Dashboard works but Gmail does not: use `doctor`; offline browsing is an
  intentional behavior, not evidence that OAuth is healthy.
