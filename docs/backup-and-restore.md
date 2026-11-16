# Backup and restore

SQLite, WAL files, logs, token files, extracted message text, and backups are
sensitive. Store backups on an encrypted volume and never commit or upload
them.

## Create and verify a backup

The online backup command is safe while the web service is serving. It includes
committed WAL data and creates a manifest:

```bash
uv run python -m app backup
uv run python -m app backup-list
```

Use `--directory PATH` to select another protected directory. A successful
backup reports the Alembic revision, SQLite integrity, foreign-key violations,
and whether the copy matches the live facts. Do not rely on a copy that is not
reported as verified.

## Preview a restore

Restore requires exclusive access because it replaces the live database file.
First inspect the candidate without writing:

```bash
uv run python -m app restore data/backups/BACKUP.sqlite3
```

The preview shows the incoming/current revisions and row-count differences.
Nothing is replaced without both explicit flags.

## Apply a restore

Generated services and every other Scribe process must release their shared
database claims. For a generated macOS installation:

```bash
uv run python -m app service uninstall
uv run python -m app restore data/backups/BACKUP.sqlite3 \
  --apply --confirm REPLACE
uv run python -m app doctor
uv run python -m app service install
```

The restore validates the candidate revision and integrity, atomically replaces
the live file, then verifies the result. It also keeps a rollback copy with a
timestamp of the database that was replaced. Record its printed path until the
restored installation has been exercised end to end.

Do not delete lock files, kill SQLite mid-write, or copy only the main database
while ignoring a live WAL. If a foreground command still holds the database,
let it finish and retry.

## Migration rehearsal

Developers must rehearse upgrade/downgrade on a copy, not the formal database.
The Alembic environment deliberately rejects a command without an explicit
target. Use `-x database=/absolute/path/to/scratch.sqlite3`, a programmatic URL,
or `SCRIBE_DATABASE_PATH` for an isolated copy. Run integrity and foreign-key
checks at every stage and compare protected row counts.

Published migration revisions are immutable. A correction belongs in the next
revision and must support every schema shape that an already-published revision
could have left behind.
