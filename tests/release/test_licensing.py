"""Licensing and governance files required by a public Scribe release."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


def _read(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_project_declares_agpl_3_or_later() -> None:
    metadata = tomllib.loads(_read("pyproject.toml"))

    assert metadata["project"]["license"] == "AGPL-3.0-or-later"
    assert "GNU AFFERO GENERAL PUBLIC LICENSE" in _read("LICENSE")


def test_every_vendored_browser_asset_has_a_notice() -> None:
    notices = _read("THIRD_PARTY_NOTICES.md")

    assert "Chart.js" in notices
    assert "HTMX" in notices
    assert (ROOT / "app/presentation/web/static/vendor/CHARTJS-LICENSE.md").is_file()
    assert (ROOT / "app/presentation/web/static/vendor/HTMX-LICENSE.txt").is_file()


@pytest.mark.parametrize("name", ["SECURITY.md", "CONTRIBUTING.md", "CHANGELOG.md"])
def test_public_governance_file_exists(name: str) -> None:
    assert (ROOT / name).is_file()


def test_security_policy_forbids_private_issue_attachments() -> None:
    policy = _read("SECURITY.md").lower()

    for private_material in (
        "credentials",
        "database",
        "logs",
        "statement",
        "email content",
    ):
        assert private_material in policy
    assert "do not upload" in policy


def test_contribution_policy_requires_synthetic_financial_fixtures() -> None:
    policy = _read("CONTRIBUTING.md").lower()

    assert "synthetic" in policy
    assert "fixture" in policy
    assert "real financial" in policy
    assert "must not" in policy
