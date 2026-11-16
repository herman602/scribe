"""Public GitHub automation must be hermetic and privacy-safe."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = (
    ROOT / ".github" / "workflows" / "quality.yml",
    ROOT / ".github" / "workflows" / "secrets.yml",
)
ACTION = re.compile(r"(?m)^\s*-?\s*uses:\s*([^\s#]+)(?:\s+#.*)?$")
PINNED_ACTION = re.compile(r"^[^@\s]+@[0-9a-f]{40}$")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_workflows_exist_with_read_only_permissions_and_immutable_actions() -> None:
    for path in WORKFLOWS:
        content = _text(path)
        assert re.search(r"(?m)^permissions:\n  contents: read\s*$", content)
        assert not re.search(r"(?m)^\s+\w[\w-]*:\s*write\s*$", content)
        actions = ACTION.findall(content)
        assert actions
        assert all(PINNED_ACTION.fullmatch(action) for action in actions)


def test_quality_workflow_is_credential_free_and_runs_every_release_gate() -> None:
    content = _text(WORKFLOWS[0])
    folded = content.casefold()

    for forbidden in (
        "secrets.",
        "pull_request_target",
        "gmail",
        "cloudflare",
        "data/app.sqlite3",
    ):
        assert forbidden not in folded
    for required in (
        'python-version: "3.12"',
        "uv run ruff check .",
        "uv run ruff format --check .",
        "uv run mypy .",
        "uv run pytest -q",
        "scripts/check_public_tree.py",
        "alembic",
        "-x database=",
        "uv build",
        "scripts/verify_distribution.py",
        "scripts/smoke_installed_distribution.py",
        "RUNNER_TEMP",
        "SCRIBE_DATABASE_PATH",
    ):
        assert required in content
    assert "UV_CACHE_DIR: ${{ runner.temp }}/uv-cache" not in content


def test_secret_workflow_fetches_and_scans_full_history_with_redaction() -> None:
    content = _text(WORKFLOWS[1])

    assert "fetch-depth: 0" in content
    assert "--all" in content
    assert "--redact" in content
    assert 'GITLEAKS_ENABLE_UPLOAD_ARTIFACT: "false"' in content
    assert 'GITLEAKS_ENABLE_COMMENTS: "false"' in content
    assert "secrets." not in content.casefold()


def test_gitleaks_configuration_has_no_broad_private_data_allowlist() -> None:
    content = _text(ROOT / ".gitleaks.toml").casefold()

    assert "usedefault = true" in content
    for forbidden in (
        "credentials.json",
        "token.json",
        'path = "data',
        "tests/fixtures",
        "client_secret",
    ):
        assert forbidden not in content


def test_public_issue_forms_forbid_private_financial_attachments() -> None:
    paths = (
        ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.yml",
        ROOT / ".github" / "ISSUE_TEMPLATE" / "parser_request.yml",
    )
    for path in paths:
        content = _text(path).casefold()
        for warning in (
            "credentials",
            "database",
            "logs",
            "real email",
            "pdf",
            "account or card",
            "financial screenshot",
        ):
            assert warning in content

    parser_form = _text(paths[1])
    assert "docs/adding-bank-parser.md" in parser_form
    assert "synthetic" in parser_form.casefold()
    assert "do not forward" in parser_form.casefold()
    assert "blank_issues_enabled: false" in _text(
        ROOT / ".github" / "ISSUE_TEMPLATE" / "config.yml"
    )


def test_pull_request_template_requires_license_privacy_and_quality_evidence() -> None:
    content = _text(ROOT / ".github" / "pull_request_template.md").casefold()

    for requirement in (
        "agpl-3.0-or-later",
        "synthetic",
        "migration",
        "ruff",
        "mypy",
        "pytest",
        "git diff --check",
    ):
        assert requirement in content


def test_contributing_points_parser_authors_to_the_complete_guide() -> None:
    content = _text(ROOT / "CONTRIBUTING.md")

    assert "docs/adding-bank-parser.md" in content
