"""One cross-process lock guarding the local database file.

`holders()` used to guess from a non-empty write-ahead log and a failed
`BEGIN EXCLUSIVE`. Neither proves anything in WAL mode: an idle connection
holds no lock at all, and `BEGIN EXCLUSIVE` does not block readers. A restore
that trusted those heuristics would replace a database another process was
still using (ADR-B100).

This is a POSIX advisory lock on a sidecar file instead. The serving process
holds it shared for its whole life; a restore takes it exclusively or refuses.
The kernel releases it if a process dies, so a crash cannot leave the database
permanently un-restorable.
"""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType

from app.domain.errors import DatabaseError

LOCK_SUFFIX = ".lock"


def lock_path(database_path: Path) -> Path:
    """Return the advisory lock file that guards one database."""
    return database_path.with_name(database_path.name + LOCK_SUFFIX)


class DatabaseInUseError(DatabaseError):
    """Another process is still using the database this operation needs."""


class SharedDatabaseLock:
    """A shared claim held for as long as a process serves the database.

    Shared rather than exclusive, so several readers — the Web process and a
    CLI command — coexist. Only a restore needs to exclude them all.
    """

    def __init__(self, database_path: Path) -> None:
        self._path = lock_path(database_path)
        self._descriptor: int | None = None

    def acquire(self) -> None:
        """Take the shared lock, creating its file if this is the first user."""
        self._path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(self._path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError as error:
            os.close(descriptor)
            raise DatabaseInUseError(
                "the database is being restored right now; try again shortly"
            ) from error
        self._descriptor = descriptor

    def release(self) -> None:
        """Release the claim; the kernel would do this anyway if we died."""
        if self._descriptor is None:
            return
        try:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
        finally:
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self) -> SharedDatabaseLock:
        self.acquire()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.release()


@contextmanager
def exclusive_database_lock(database_path: Path) -> Iterator[None]:
    """Hold the database exclusively, or refuse.

    Deliberately not forceable. A flag cannot make it safe to overwrite a file
    another process is writing through, so there is nothing for an override to
    mean here.
    """
    path = lock_path(database_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise DatabaseInUseError(
                "the database is still in use by another process. Every reader "
                "holds this claim, so stop the local service and let any "
                "scheduled sync finish:\n"
                "  uv run python -m app service uninstall\n"
                "Reinstall the generated services after the exclusive operation."
            ) from error
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)
