"""Inspect wheel and source archives without extracting them."""

from __future__ import annotations

import sys
import tarfile
import zipfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from scripts.check_public_tree import scan_public_member

ROOT = Path(__file__).resolve().parents[1]
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_CONTENT_BYTES = 256 * 1024 * 1024


@dataclass(frozen=True)
class DistributionReport:
    """Safe, deterministic artifact findings containing paths but no secrets."""

    required_missing: tuple[str, ...]
    forbidden_paths: tuple[str, ...]
    forbidden_literals: tuple[str, ...]

    @property
    def ok(self) -> bool:
        """Return true only when every completeness and privacy rule passes."""
        return not (
            self.required_missing or self.forbidden_paths or self.forbidden_literals
        )


@dataclass(frozen=True)
class _ArchiveMember:
    name: str
    size: int
    content: bytes | None
    unsafe: bool = False


def _default_required_members() -> tuple[str, ...]:
    runtime_files = sorted(
        path.relative_to(ROOT).as_posix()
        for path in (ROOT / "app").rglob("*")
        if path.is_file()
        and "__pycache__" not in path.parts
        and path.suffix not in {".pyc", ".pyo"}
    )
    return (*runtime_files, "LICENSE", "THIRD_PARTY_NOTICES.md")


def _normalized_name(raw_name: str) -> str | None:
    if not raw_name or "\\" in raw_name or "\0" in raw_name:
        return None
    path = PurePosixPath(raw_name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _source_prefix(names: Sequence[str]) -> str | None:
    first_parts = {PurePosixPath(name).parts[0] for name in names if name}
    if len(first_parts) != 1:
        return None
    candidate = next(iter(first_parts))
    if candidate.casefold().replace("-", "_").startswith("scribe_local_"):
        return f"{candidate}/"
    return None


def _canonical_name(name: str, prefix: str | None) -> str:
    return name.removeprefix(prefix) if prefix else name


def _zip_members(path: Path) -> tuple[_ArchiveMember, ...]:
    members: list[_ArchiveMember] = []
    with zipfile.ZipFile(path) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            normalized = _normalized_name(info.filename)
            if normalized is None:
                members.append(
                    _ArchiveMember(info.filename, info.file_size, None, unsafe=True)
                )
                continue
            if info.file_size > MAX_MEMBER_BYTES:
                members.append(_ArchiveMember(normalized, info.file_size, None))
                continue
            members.append(
                _ArchiveMember(normalized, info.file_size, archive.read(info))
            )
    return tuple(members)


def _tar_members(path: Path) -> tuple[_ArchiveMember, ...]:
    members: list[_ArchiveMember] = []
    with tarfile.open(path, mode="r:*") as archive:
        for info in archive.getmembers():
            if info.isdir():
                continue
            normalized = _normalized_name(info.name)
            if normalized is None or not info.isfile():
                members.append(_ArchiveMember(info.name, info.size, None, unsafe=True))
                continue
            if info.size > MAX_MEMBER_BYTES:
                members.append(_ArchiveMember(normalized, info.size, None))
                continue
            stream = archive.extractfile(info)
            if stream is None:
                members.append(_ArchiveMember(normalized, info.size, None, unsafe=True))
                continue
            members.append(_ArchiveMember(normalized, info.size, stream.read()))
    return tuple(members)


def _read_members(path: Path) -> tuple[_ArchiveMember, ...]:
    if zipfile.is_zipfile(path):
        return _zip_members(path)
    if tarfile.is_tarfile(path):
        return _tar_members(path)
    raise ValueError("artifact is neither a ZIP wheel nor a TAR source archive")


def _satisfies(required: str, available: set[str]) -> bool:
    if required in available:
        return True
    if required in {"LICENSE", "THIRD_PARTY_NOTICES.md"}:
        suffix = f".dist-info/licenses/{required}"
        return any(name.endswith(suffix) for name in available)
    return False


def inspect_archive(
    path: Path,
    *,
    required_members: Sequence[str] | None = None,
) -> DistributionReport:
    """Inspect one artifact in memory; never extract or print matched content."""
    members = _read_members(path)
    safe_names = [member.name for member in members if not member.unsafe]
    prefix = _source_prefix(safe_names)
    available: set[str] = set()
    forbidden_paths: set[str] = set()
    forbidden_literals: set[str] = set()
    total = 0

    for member in members:
        canonical = _canonical_name(member.name, prefix)
        total += member.size
        if member.unsafe or member.size > MAX_MEMBER_BYTES:
            forbidden_paths.add(canonical)
            continue
        available.add(canonical)
        assert member.content is not None
        findings = scan_public_member(PurePosixPath(canonical), member.content)
        for finding in findings:
            if finding.rule == "private-path":
                forbidden_paths.add(canonical)
            else:
                forbidden_literals.add(canonical)

    if total > MAX_ARCHIVE_CONTENT_BYTES:
        forbidden_paths.add("<archive-content-size>")

    required = tuple(required_members or _default_required_members())
    missing = tuple(
        sorted(item for item in required if not _satisfies(item, available))
    )
    return DistributionReport(
        required_missing=missing,
        forbidden_paths=tuple(sorted(forbidden_paths)),
        forbidden_literals=tuple(sorted(forbidden_literals)),
    )


def _emit(path: Path, report: DistributionReport) -> None:
    for member in report.required_missing:
        print(f"{path}: missing {member}")
    for member in report.forbidden_paths:
        print(f"{path}: forbidden path {member}")
    for member in report.forbidden_literals:
        print(f"{path}: forbidden content in {member}")


def main(
    argv: Sequence[str] | None = None,
    *,
    required_members: Sequence[str] | None = None,
) -> int:
    """Verify every named artifact, returning nonzero on any safe failure."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if not arguments:
        print("usage: verify_distribution.py ARTIFACT [...]", file=sys.stderr)
        return 2
    failed = False
    for argument in arguments:
        path = Path(argument)
        try:
            report = inspect_archive(path, required_members=required_members)
        except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile):
            print(f"{path}: artifact inspection failed")
            failed = True
            continue
        _emit(path, report)
        failed = failed or not report.ok
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
