"""Contracts for the public self-hosting documentation."""

from __future__ import annotations

import re
import struct
import tomllib
from pathlib import Path

import pytest
from app.infrastructure.config.settings import AppSettings

ROOT = Path(__file__).resolve().parents[2]
DOCS = (
    "installation.md",
    "gmail-oauth.md",
    "configuration.md",
    "banks-and-parsers.md",
    "adding-bank-parser.md",
    "backup-and-restore.md",
    "macos-service.md",
)
_MARKDOWN_LINK = re.compile(r"\[[^]]+\]\(([^)]+)\)")


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def test_readme_is_a_complete_but_honest_public_entry_point() -> None:
    readme = _text("README.md")

    for command in (
        "uv sync --extra dev",
        "uv run python -m app init",
        "uv run python -m app auth",
        "uv run python -m app doctor",
        "uv run python -m app serve",
    ):
        assert command in readme
    for document in DOCS:
        assert f"docs/{document}" in readme
    assert "technical self-hosters" in readme
    assert "no hosted service" in readme.lower()
    assert "no one-click installer" in readme.lower()
    assert "docs/assets/dashboard-overview.png" in readme


def test_public_metadata_and_installation_name_the_canonical_repository() -> None:
    metadata = tomllib.loads(_text("pyproject.toml"))
    urls = metadata["project"]["urls"]

    assert urls["Repository"] == "https://github.com/herman602/scribe"
    assert urls["Issues"] == "https://github.com/herman602/scribe/issues"
    assert urls["Homepage"] == "https://scribe-project.andylab.dev"
    assert "https://scribe-project.andylab.dev" in _text("README.md")
    installation = _text("docs/installation.md")
    assert "git clone https://github.com/herman602/scribe.git" in installation
    assert "YOUR_ACCOUNT" not in installation


def test_v0_1_0_changelog_has_the_publication_date() -> None:
    changelog = _text("CHANGELOG.md")

    assert "## 0.1.0 - 2026-11-16" in changelog
    assert "## 0.1.0 - Unreleased" not in changelog


@pytest.mark.parametrize("path", ["README.md", "PRIVACY.md"])
def test_public_entry_docs_have_no_stale_milestone_claims(path: str) -> None:
    content = _text(path).casefold()

    for stale in (
        "current milestone",
        "b3 onward is still design",
        "backup cli arrives in milestone 10",
    ):
        assert stale not in content


def test_oauth_guide_requires_user_owned_desktop_credentials_and_exact_scopes() -> None:
    guide = _text("docs/gmail-oauth.md")

    assert "user-owned" in guide
    assert "Desktop app" in guide
    assert "https://www.googleapis.com/auth/gmail.readonly" in guide
    assert "https://www.googleapis.com/auth/gmail.modify" in guide
    assert "processed-message labeling" in guide
    assert "chmod 600 credentials.json" in guide
    assert "maintainer OAuth client" not in guide
    assert "download our client" not in guide.casefold()


def test_configuration_guide_names_every_public_settings_field() -> None:
    guide = _text("docs/configuration.md")

    missing = [name for name in AppSettings.model_fields if f"`{name}`" not in guide]
    assert missing == []
    for heading in (
        "Paths and local storage",
        "Gmail and parsing",
        "Web access",
        "Synchronization and maintenance",
        "Money and local time",
    ):
        assert heading in guide


def test_parser_guide_names_supported_shapes_without_promising_bank_apis() -> None:
    guide = _text("docs/banks-and-parsers.md")

    for institution in (
        "Taipei Fubon",
        "Sunny Bank",
        "Cathay United Bank",
        "First Bank",
        "Standard Chartered Taiwan",
        "SinoPac",
        "Hua Nan Commercial Bank",
    ):
        assert institution in guide
    for shape in ("plain-text email", "HTML email", "encrypted text PDF"):
        assert shape in guide
    assert "not a bank API guarantee" in guide
    assert "adding-bank-parser.md" in guide


def test_bank_parser_contribution_guide_covers_the_real_extension_contract() -> None:
    guide = _text("docs/adding-bank-parser.md")

    for contract in (
        "TransactionParser",
        "ParserEmail",
        "ParseResult",
        "TransactionCandidate",
        "DefaultParserRegistry",
        "_build_parser_pipeline",
    ):
        assert f"`{contract}`" in guide
    for boundary in (
        "one Gmail message",
        "thread",
        "sender-rules",
        "encrypted text PDF",
        "OS credential store",
        "synthetic fixture",
        "tests/test_bank_parsers.py",
        "uv run pytest",
        "Prompt for an LLM",
        "Do not access a live Gmail account",
    ):
        assert boundary in guide


def test_backup_guide_uses_real_commands_and_explains_exclusive_restore() -> None:
    guide = _text("docs/backup-and-restore.md")
    normalized = guide.casefold()

    for command in (
        "python -m app backup",
        "python -m app backup-list",
        "python -m app restore",
    ):
        assert command in guide
    assert "service uninstall" in normalized
    assert "service install" in normalized
    assert "restore requires exclusive" in normalized
    assert "rollback copy" in normalized


def test_public_dashboard_screenshot_is_a_real_large_png() -> None:
    path = ROOT / "docs/assets/dashboard-overview.png"
    content = path.read_bytes()

    assert content[:8] == b"\x89PNG\r\n\x1a\n"
    assert content[12:16] == b"IHDR"
    width, height = struct.unpack(">II", content[16:24])
    assert width >= 720
    assert height >= 480


def test_public_documentation_has_no_broken_relative_links() -> None:
    paths = [ROOT / "README.md", *(ROOT / "docs" / name for name in DOCS)]
    broken: list[str] = []

    for source in paths:
        for target in _MARKDOWN_LINK.findall(source.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            relative = target.split("#", 1)[0]
            if relative and not (source.parent / relative).resolve().exists():
                broken.append(f"{source.relative_to(ROOT)} -> {target}")

    assert broken == []


def test_internal_release_plans_are_not_public_sources() -> None:
    assert not (ROOT / "docs/superpowers").exists()
    assert "docs/superpowers/" in _text(".gitignore")
