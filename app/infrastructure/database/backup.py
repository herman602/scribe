"""Verified local backups and an explicit, reversible restore.

A backup is taken with SQLite's online backup API rather than by copying the
file. A plain copy omits the `-wal` sidecar, so it silently produces a
*stale* database with no error anywhere — the worst possible failure for the
one artefact that exists to be trusted (ADR-B098).

Every backup is verified immediately after it is written, by comparing the
copy's revision, integrity, foreign keys, and row counts against the live
database. An unverified backup is worse than none, because it is believed.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from app import __version__
from app.domain.errors import DatabaseError
from app.infrastructure.database.locking import exclusive_database_lock

#: Tables whose row counts must survive a backup and a restore unchanged.
#: Money first, then the Gmail evidence it was derived from.
COUNTED_TABLES = (
    "financial_accounts",
    "wallets",
    "journal_entries",
    "journal_postings",
    "financial_events",
    "event_allocations",
    "cards",
    "scheduled_transfers",
    "emails",
    "transactions",
    "import_batches",
    "import_rows",
)

MANIFEST_SUFFIX = ".manifest.json"


@dataclass(frozen=True)
class DatabaseFacts:
    """What a database says about itself, for comparing two of them."""

    revision: str | None
    integrity: str
    foreign_key_violations: int
    counts: dict[str, int]
    currency_totals: dict[str, int]

    @property
    def is_sound(self) -> bool:
        """Return whether the file passes SQLite's own structural checks."""
        return self.integrity == "ok" and self.foreign_key_violations == 0

    @property
    def journal_balances(self) -> bool:
        """Return whether every currency still nets to zero."""
        return all(total == 0 for total in self.currency_totals.values())


@dataclass(frozen=True)
class BackupResult:
    """One written backup and the comparison that verified it."""

    path: Path
    manifest_path: Path
    live: DatabaseFacts
    copy: DatabaseFacts

    @property
    def verified(self) -> bool:
        """Return whether the copy matches the database it was taken from.

        Structural soundness is not enough: a file whose journal does not net
        to zero is a broken ledger that SQLite is perfectly happy with, and a
        backup is only worth taking if restoring it would be safe.
        """
        return (
            self.live == self.copy and self.copy.is_sound and self.copy.journal_balances
        )


def read_facts(path: Path) -> DatabaseFacts:
    """Describe one database file without modifying it."""
    if not path.is_file():
        raise DatabaseError(f"no database at {path}")
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return _facts(connection)
    except sqlite3.DatabaseError as error:
        # A file that is not a database must fail as a refusal, not as a
        # driver error escaping through a restore.
        raise DatabaseError(f"{path} is not a readable SQLite database") from error
    finally:
        connection.close()


def _facts(connection: sqlite3.Connection) -> DatabaseFacts:
    """Describe one open connection's database."""
    revision_row = connection.execute(
        "SELECT version_num FROM alembic_version"
    ).fetchone()
    counts: dict[str, int] = {}
    for table in COUNTED_TABLES:
        try:
            counts[table] = int(
                connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            )
        except sqlite3.OperationalError:
            # A backup taken before a table existed is still a valid
            # backup; it simply has nothing to say about that table.
            continue
    totals = {
        str(currency): int(total or 0)
        for currency, total in connection.execute(
            "SELECT currency, SUM(amount_minor) FROM journal_postings GROUP BY currency"
        )
    }
    return DatabaseFacts(
        revision=None if revision_row is None else str(revision_row[0]),
        integrity=str(connection.execute("PRAGMA integrity_check").fetchone()[0]),
        foreign_key_violations=len(
            connection.execute("PRAGMA foreign_key_check").fetchall()
        ),
        counts=counts,
        currency_totals=totals,
    )


def create_backup(
    source: Path, directory: Path, *, now: datetime | None = None
) -> BackupResult:
    """Copy the live database through SQLite's backup API and verify the copy."""
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    target = directory / f"app-{stamp}.sqlite3"
    # Two backups inside one second are a real case — a rollback copy is
    # taken immediately before a restore — so the name gains a suffix rather
    # than the second one failing.
    attempt = 1
    while target.exists():
        attempt += 1
        target = directory / f"app-{stamp}-{attempt}.sqlite3"

    live = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    copy = sqlite3.connect(target)
    try:
        live.backup(copy)
    finally:
        copy.close()
        live.close()
    target.chmod(0o600)

    facts = read_facts(source)
    copied = read_facts(target)
    manifest_path = target.with_suffix(MANIFEST_SUFFIX)
    manifest_path.write_text(
        json.dumps(
            {
                "created_at": (now or datetime.now(UTC)).isoformat(),
                "app_version": __version__,
                "source": str(source),
                "backup": str(target),
                "source_bytes": source.stat().st_size,
                "backup_bytes": target.stat().st_size,
                "live": asdict(facts),
                "backup_verified": asdict(copied),
                "matches_live": facts == copied,
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    manifest_path.chmod(0o600)
    return BackupResult(
        path=target, manifest_path=manifest_path, live=facts, copy=copied
    )


def list_backups(directory: Path) -> tuple[Path, ...]:
    """Return every backup in one directory, newest first."""
    if not directory.is_dir():
        return ()
    return tuple(
        sorted(
            (item for item in directory.glob("*.sqlite3") if item.is_file()),
            key=lambda item: item.stat().st_mtime,
            reverse=True,
        )
    )


def check_restorable(
    facts: DatabaseFacts, *, expected_revision: str | None
) -> tuple[str, ...]:
    """Return every reason this database must not replace a live one.

    Checked *before* anything is overwritten. A structurally valid SQLite file
    can still hold an unbalanced ledger or a schema this build cannot run, and
    discovering either after the overwrite leaves the user restoring a rollback
    copy to undo a restore (ADR-B098).
    """
    problems: list[str] = []
    if facts.integrity != "ok":
        problems.append(f"integrity_check reported {facts.integrity}")
    if facts.foreign_key_violations:
        problems.append(f"{facts.foreign_key_violations} foreign-key violation(s)")
    if not facts.journal_balances:
        unbalanced = {
            currency: total
            for currency, total in facts.currency_totals.items()
            if total != 0
        }
        problems.append(f"the journal does not balance: {unbalanced}")
    if facts.revision is None:
        problems.append("no Alembic revision, so its schema is unknown")
    elif expected_revision is not None and facts.revision != expected_revision:
        problems.append(
            f"revision {facts.revision} is not the {expected_revision} this "
            "build runs; migrate a copy first"
        )
    return tuple(problems)


def restore_backup(
    backup: Path,
    destination: Path,
    rollback_directory: Path,
    *,
    expected_revision: str | None = None,
) -> Path:
    """Replace the live database only after the incoming one has been proved.

    The incoming database is copied to a temporary file beside the
    destination, verified there, and only then moved into place with
    ``os.replace``, which is atomic on one filesystem. The live file is never
    partially written and its write-ahead log is never deleted underneath a
    process that may still hold it open.
    """
    problems = check_restorable(read_facts(backup), expected_revision=expected_revision)
    if problems:
        raise DatabaseError(
            "refusing to restore: " + "; ".join(problems) + ". Nothing was changed."
        )

    rollback: Path | None = None
    with exclusive_database_lock(destination):
        if destination.is_file():
            rollback = create_backup(destination, rollback_directory).path

        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        staged = destination.with_name(f"{destination.name}.restoring")
        staged.unlink(missing_ok=True)
        replaced = False
        try:
            source = sqlite3.connect(f"file:{backup}?mode=ro", uri=True)
            target = sqlite3.connect(staged)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            staged.chmod(0o600)

            # Prove the staged file before it becomes the live one.
            staged_problems = check_restorable(
                read_facts(staged), expected_revision=expected_revision
            )
            if staged_problems:
                raise DatabaseError(
                    "the staged copy failed its checks: "
                    + "; ".join(staged_problems)
                    + ". Nothing was replaced."
                )
            os.replace(staged, destination)
            replaced = True
        finally:
            staged.unlink(missing_ok=True)
            if replaced:
                # These sidecars describe the file that is now gone. Removing
                # them on a failed restore would instead discard committed
                # data belonging to the database still in place.
                for sidecar in ("-wal", "-shm"):
                    destination.with_name(destination.name + sidecar).unlink(
                        missing_ok=True
                    )
    return rollback if rollback is not None else destination
