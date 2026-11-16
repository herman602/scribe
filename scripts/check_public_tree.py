"""Reject private operator material from the tracked public source tree."""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

PRIVATE_BASENAMES = frozenset({".env", "config.toml", "credentials.json", "token.json"})
PRIVATE_SUFFIXES = (
    ".db",
    ".db-shm",
    ".db-wal",
    ".sqlite",
    ".sqlite-shm",
    ".sqlite-wal",
    ".sqlite3",
    ".sqlite3-shm",
    ".sqlite3-wal",
)
_OPERATOR_PATTERNS = (
    re.compile(r"/Users/[^/\s]+/Documents/scribe(?:/|\b)"),
    re.compile(r"\bscribe\.(?!example\.com\b)[a-z0-9.-]+\.[a-z]{2,}\b", re.I),
    re.compile(
        r"\b[a-z0-9._%+-]+@"
        r"(?!(?:[a-z0-9-]+\.)*(?:invalid|test|example)\b)"
        r"(?!example\.com\b)"
        r"[a-z0-9.-]+\.[a-z]{2,}\b",
        re.I,
    ),
)
_PLACEHOLDER_MARKERS = (
    "dummy",
    "example",
    "fake",
    "fixture",
    "redacted",
    "replace",
    "synthetic",
    "test",
)
_PRIVATE_KEY_HEADER = re.compile(rb"-----BEGIN (?:[A-Z0-9 ]+ )?PRIVATE KEY-----")
_JSON_CREDENTIAL = re.compile(
    r'"(?:access_token|client_secret|private_key|refresh_token)"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"'
)


@dataclass(frozen=True, order=True)
class Finding:
    """One private-tree rule violation, without the matched content."""

    path: str
    rule: str
    line: int | None = None


def _relative_path(root: Path, path: Path) -> PurePosixPath:
    return PurePosixPath(path.resolve().relative_to(root.resolve()).as_posix())


def _is_private_path(path: PurePosixPath) -> bool:
    name = path.name.casefold()
    return (
        name in PRIVATE_BASENAMES
        or any(part.casefold() == "data" for part in path.parts)
        or name.endswith(PRIVATE_SUFFIXES)
    )


def _line_number(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


def _credential_line(content: str, raw: bytes) -> int | None:
    private_key = _PRIVATE_KEY_HEADER.search(raw)
    if private_key is not None:
        return raw.count(b"\n", 0, private_key.start()) + 1
    for match in _JSON_CREDENTIAL.finditer(content):
        value = match.group(1).casefold()
        if value and not any(marker in value for marker in _PLACEHOLDER_MARKERS):
            return _line_number(content, match.start())
    return None


def scan_public_member(path: PurePosixPath, raw: bytes) -> tuple[Finding, ...]:
    """Inspect one logical public-tree member without exposing matched values."""
    relative_text = path.as_posix()
    if _is_private_path(path):
        return (Finding(relative_text, "private-path"),)
    if b"\0" in raw:
        return ()
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError:
        return ()
    literal_match = next(
        (match for pattern in _OPERATOR_PATTERNS if (match := pattern.search(content))),
        None,
    )
    if literal_match is not None:
        return (
            Finding(
                relative_text,
                "maintainer-literal",
                _line_number(content, literal_match.start()),
            ),
        )
    credential_line = _credential_line(content, raw)
    if credential_line is not None:
        return (Finding(relative_text, "credential-content", credential_line),)
    return ()


def scan_public_tree(root: Path, tracked_paths: Sequence[Path]) -> tuple[Finding, ...]:
    """Return deterministic findings for explicitly supplied tracked paths."""
    findings: list[Finding] = []
    for path in tracked_paths:
        relative = _relative_path(root, path)
        raw = path.read_bytes()
        findings.extend(scan_public_member(relative, raw))

    return tuple(
        sorted(findings, key=lambda item: (item.path, item.rule, item.line or 0))
    )


def _tracked_paths(root: Path) -> tuple[Path, ...]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return tuple(
        root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item
    )


def main() -> int:
    """Scan the current Git worktree without reading ignored local files."""
    try:
        root_result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            check=True,
            capture_output=True,
            text=True,
        )
        root = Path(root_result.stdout.strip())
        findings = scan_public_tree(root, _tracked_paths(root))
    except (OSError, subprocess.SubprocessError, UnicodeError, ValueError):
        print("public-tree scan failed", file=sys.stderr)
        return 1

    for finding in findings:
        location = finding.path
        if finding.line is not None:
            location = f"{location}:{finding.line}"
        print(f"{location} {finding.rule}")
    return int(bool(findings))


if __name__ == "__main__":
    raise SystemExit(main())
