"""The public repository must not contain local operator material."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from scripts.check_public_tree import scan_public_tree

ROOT = Path(__file__).resolve().parents[2]


def _write(root: Path, names: list[str]) -> list[Path]:
    paths: list[Path] = []
    for name in names:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic", encoding="utf-8")
        paths.append(path)
    return paths


def _write_text(root: Path, name: str, value: str) -> Path:
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value, encoding="utf-8")
    return path


def _tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        check=True,
        capture_output=True,
    )
    return [root / item.decode() for item in result.stdout.split(b"\0") if item]


def test_scanner_rejects_private_artifact_names(tmp_path: Path) -> None:
    paths = _write(
        tmp_path,
        ["credentials.json", "data/app.sqlite3", "scratch.db-wal", ".env"],
    )

    assert {finding.rule for finding in scan_public_tree(tmp_path, paths)} == {
        "private-path"
    }


@pytest.mark.parametrize(
    "literal",
    [
        "/Users/" + "alice/Documents/scribe",
        "scribe.private-" + "domain.net",
        "owner@private-" + "domain.net",
    ],
)
def test_scanner_rejects_maintainer_literals(tmp_path: Path, literal: str) -> None:
    path = _write_text(tmp_path, "tracked.txt", literal)

    findings = scan_public_tree(tmp_path, [path])

    assert [(finding.path, finding.rule, finding.line) for finding in findings] == [
        ("tracked.txt", "maintainer-literal", 1)
    ]


def test_scanner_accepts_reserved_examples(tmp_path: Path) -> None:
    path = _write_text(
        tmp_path,
        "tracked.txt",
        "https://scribe.example.com notice@bank.invalid owner@example.com",
    )

    assert scan_public_tree(tmp_path, [path]) == ()


@pytest.mark.parametrize(
    "value",
    [
        "-----BEGIN " + "PRIVATE KEY-----",
        '{"client_' + 'secret": "real-looking-secret-value"}',
        '{"refresh_' + 'token": "real-looking-refresh-value"}',
    ],
)
def test_scanner_rejects_credential_content(tmp_path: Path, value: str) -> None:
    path = _write_text(tmp_path, "tracked.txt", value)

    findings = scan_public_tree(tmp_path, [path])

    assert len(findings) == 1
    assert findings[0].rule == "credential-content"


@pytest.mark.parametrize(
    "value",
    [
        '{"client_' + 'secret": "replace-me"}',
        '{"refresh_' + 'token": "fixture-only"}',
    ],
)
def test_scanner_accepts_explicit_placeholders(tmp_path: Path, value: str) -> None:
    path = _write_text(tmp_path, "tracked.txt", value)

    assert scan_public_tree(tmp_path, [path]) == ()


def test_scanner_skips_binary_content_but_still_checks_its_path(tmp_path: Path) -> None:
    safe = tmp_path / "safe.bin"
    safe.write_bytes(b"\x93\xfa")
    private = tmp_path / "data" / "private.bin"
    private.parent.mkdir()
    private.write_bytes(b"\x00synthetic")

    findings = scan_public_tree(tmp_path, [safe, private])

    assert [(finding.path, finding.rule) for finding in findings] == [
        ("data/private.bin", "private-path")
    ]


def test_actual_tracked_tree_contains_no_private_material() -> None:
    assert scan_public_tree(ROOT, _tracked_paths(ROOT)) == ()
