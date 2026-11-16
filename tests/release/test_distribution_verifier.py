"""A built artifact must be complete and safe before it can be published."""

from __future__ import annotations

import io
import tarfile
import zipfile
from collections.abc import Mapping
from pathlib import Path

import pytest
from scripts.smoke_installed_distribution import (
    isolated_environment,
    validate_doctor_report,
)
from scripts.verify_distribution import DistributionReport, inspect_archive, main

REQUIRED = (
    "app/__init__.py",
    "app/presentation/web/templates/base.html",
    "app/presentation/web/static/styles.css",
    "app/presentation/web/static/vendor/CHART_LICENSE.md",
    "app/infrastructure/database/migrations/env.py",
    "app/infrastructure/database/migrations/script.py.mako",
    "app/infrastructure/database/migrations/versions/0014_fixture.py",
    "LICENSE",
    "THIRD_PARTY_NOTICES.md",
)
ROOT = Path(__file__).resolve().parents[2]


def _safe_members() -> dict[str, bytes]:
    return {path: f"synthetic {path}".encode() for path in REQUIRED}


def _zip(path: Path, members: Mapping[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name, content in members.items():
            archive.writestr(name, content)
    return path


def _tar(path: Path, members: Mapping[str, bytes], *, prefix: str = "") -> Path:
    with tarfile.open(path, "w:gz") as archive:
        for name, content in members.items():
            info = tarfile.TarInfo(f"{prefix}{name}")
            info.size = len(content)
            archive.addfile(info, io.BytesIO(content))
    return path


@pytest.mark.parametrize(
    ("missing", "archive_name"),
    [
        ("app/presentation/web/templates/base.html", "missing-template.whl"),
        ("app/presentation/web/static/styles.css", "missing-static.whl"),
        (
            "app/infrastructure/database/migrations/versions/0014_fixture.py",
            "missing-migration.whl",
        ),
        ("LICENSE", "missing-license.whl"),
        ("THIRD_PARTY_NOTICES.md", "missing-notices.whl"),
    ],
)
def test_missing_runtime_or_license_member_is_reported_exactly(
    tmp_path: Path, missing: str, archive_name: str
) -> None:
    members = _safe_members()
    del members[missing]

    report = inspect_archive(
        _zip(tmp_path / archive_name, members), required_members=REQUIRED
    )

    assert report.required_missing == (missing,)
    assert not report.ok


def test_complete_prefixed_source_archive_passes(tmp_path: Path) -> None:
    archive = _tar(
        tmp_path / "scribe-local-0.1.0.tar.gz",
        _safe_members(),
        prefix="scribe_local-0.1.0/",
    )

    report = inspect_archive(archive, required_members=REQUIRED)

    assert report == DistributionReport((), (), ())
    assert report.ok


@pytest.mark.parametrize(
    "private_path",
    [
        "credentials.json",
        "config.toml",
        ".env",
        "data/token.json",
        "operator.sqlite3",
    ],
)
def test_private_artifact_path_is_rejected(tmp_path: Path, private_path: str) -> None:
    members = _safe_members()
    members[private_path] = b"synthetic"

    report = inspect_archive(
        _zip(tmp_path / "private.whl", members), required_members=REQUIRED
    )

    assert report.forbidden_paths == (private_path,)
    assert not report.ok


@pytest.mark.parametrize(
    "content",
    [
        b"/Users/" + b"alice/Documents/scribe",
        b"scribe.private-" + b"domain.net",
        b"owner@private-" + b"domain.net",
        b"-----BEGIN " + b"PRIVATE KEY-----\nfixture",
        b'{"refresh_' + b'token": "not-a-placeholder-value"}',
    ],
)
def test_private_literal_is_rejected_without_echoing_it(
    tmp_path: Path, content: bytes, capsys: pytest.CaptureFixture[str]
) -> None:
    members = _safe_members()
    members["app/private.py"] = content
    archive = _zip(tmp_path / "literal.whl", members)

    report = inspect_archive(archive, required_members=REQUIRED)

    assert report.forbidden_literals == ("app/private.py",)
    assert content.decode() not in repr(report)
    assert main([str(archive)], required_members=REQUIRED) == 1
    output = capsys.readouterr()
    assert content.decode() not in output.out
    assert content.decode() not in output.err


@pytest.mark.parametrize("archive_kind", ["zip", "tar"])
def test_path_traversal_is_rejected_without_extraction(
    tmp_path: Path, archive_kind: str
) -> None:
    members = _safe_members()
    members["../../token.json"] = b"secret"
    archive = (
        _zip(tmp_path / "traversal.whl", members)
        if archive_kind == "zip"
        else _tar(tmp_path / "traversal.tar.gz", members)
    )

    report = inspect_archive(archive, required_members=REQUIRED)

    assert report.forbidden_paths == ("../../token.json",)
    assert not (tmp_path.parent / "token.json").exists()


def test_oversized_member_is_rejected_before_reading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scripts import verify_distribution

    monkeypatch.setattr(verify_distribution, "MAX_MEMBER_BYTES", 1000)
    members = _safe_members()
    members["app/large.txt"] = b"x" * 1001

    report = inspect_archive(
        _zip(tmp_path / "large.whl", members), required_members=REQUIRED
    )

    assert report.forbidden_paths == ("app/large.txt",)


def test_installed_smoke_environment_discards_every_inherited_scribe_value(
    tmp_path: Path,
) -> None:
    env = isolated_environment(
        tmp_path,
        inherited={
            "PATH": "/synthetic/bin",
            "SCRIBE_DATABASE_PATH": "/private/live.sqlite3",
            "SCRIBE_GMAIL_TOKEN_PATH": "/private/token.json",
            "UNRELATED": "kept",
        },
    )

    assert env["PATH"] == "/synthetic/bin"
    assert env["UNRELATED"] == "kept"
    assert env["SCRIBE_DATABASE_PATH"] == str(tmp_path / "data" / "app.sqlite3")
    assert env["SCRIBE_GMAIL_TOKEN_PATH"] == str(tmp_path / "data" / "token.json")
    assert "/private/live.sqlite3" not in env.values()
    assert "/private/token.json" not in env.values()


def test_installed_smoke_accepts_only_the_documented_missing_oauth_failures() -> None:
    validate_doctor_report(
        {
            "checks": [
                {"name": "Python version", "status": "PASS", "detail": "3.12"},
                {"name": "config.toml", "status": "PASS", "detail": "config"},
                {
                    "name": "data directory writable",
                    "status": "PASS",
                    "detail": "data",
                },
                {"name": "SQLite connection", "status": "PASS", "detail": "ok"},
                {"name": "migration status", "status": "PASS", "detail": "head"},
                {
                    "name": "Gmail credentials",
                    "status": "FAIL",
                    "detail": "not found",
                },
                {"name": "Gmail token", "status": "FAIL", "detail": "not found"},
                {
                    "name": "Gmail API connection",
                    "status": "WARN",
                    "detail": "skipped",
                },
            ]
        }
    )


def test_installed_smoke_rejects_a_database_failure_disguised_as_doctor_output() -> (
    None
):
    with pytest.raises(AssertionError, match="SQLite connection"):
        validate_doctor_report(
            {
                "checks": [
                    {
                        "name": "Python version",
                        "status": "PASS",
                        "detail": "3.12",
                    },
                    {"name": "config.toml", "status": "PASS", "detail": "config"},
                    {
                        "name": "data directory writable",
                        "status": "PASS",
                        "detail": "data",
                    },
                    {
                        "name": "SQLite connection",
                        "status": "FAIL",
                        "detail": "broken",
                    },
                    {
                        "name": "migration status",
                        "status": "PASS",
                        "detail": "head",
                    },
                    {
                        "name": "Gmail credentials",
                        "status": "FAIL",
                        "detail": "not found",
                    },
                    {
                        "name": "Gmail token",
                        "status": "FAIL",
                        "detail": "not found",
                    },
                    {
                        "name": "Gmail API connection",
                        "status": "WARN",
                        "detail": "skipped",
                    },
                ]
            }
        )


def test_distribution_release_commands_are_documented() -> None:
    guide = (ROOT / "DEVELOPMENT.md").read_text(encoding="utf-8")

    for command in (
        "uv build",
        "scripts/verify_distribution.py",
        "scripts/smoke_installed_distribution.py",
        "--forbid-import-root",
    ):
        assert command in guide
