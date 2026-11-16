"""Pure sender-rule validation, matching, and query tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from app.application.services.sender_rule_management import ManageSenderRules
from app.domain.enums import SenderMatchType
from app.domain.errors import SenderRuleConfigurationError
from app.domain.models import SenderRule
from app.domain.sender_rules import (
    compile_gmail_query,
    match_sender,
    normalize_sender_match,
    validate_institution_key,
)
from app.infrastructure.database.engine import Database
from app.infrastructure.database.unit_of_work import SqlAlchemyUnitOfWork

_NOW = datetime(2026, 7, 27, tzinfo=UTC)


def _rule(
    rule_id: int,
    match_type: SenderMatchType,
    value: str,
    *,
    priority: int = 100,
    gmail_query_enabled: bool = True,
) -> SenderRule:
    return SenderRule(
        id=rule_id,
        gmail_account_id=1,
        institution_key=f"bank_{rule_id}",
        institution_name=f"測試銀行 {rule_id}",
        match_type=match_type,
        match_value_normalized=value,
        parser_name=None,
        priority=priority,
        enabled=True,
        gmail_query_enabled=gmail_query_enabled,
        version=1,
        created_at=_NOW,
        updated_at=_NOW,
    )


def test_exact_and_domain_rules_match_rfc_from_header_by_priority() -> None:
    domain = _rule(1, SenderMatchType.DOMAIN_SUFFIX, "bank.example", priority=20)
    exact = _rule(
        2,
        SenderMatchType.EXACT_ADDRESS,
        "notice@alerts.bank.example",
        priority=10,
    )

    matched = match_sender(
        '"測試通知" <NOTICE@alerts.bank.example>',
        (domain, exact),
    )

    assert matched == exact
    assert match_sender("other@example.test", (domain, exact)) is None


def test_query_compiler_uses_only_validated_discovery_rules() -> None:
    rules = (
        _rule(1, SenderMatchType.EXACT_ADDRESS, "notice@bank.example"),
        _rule(
            2,
            SenderMatchType.DOMAIN_SUFFIX,
            "payments.example",
            gmail_query_enabled=False,
        ),
    )

    query = compile_gmail_query(
        rules,
        custom_query="newer_than:2y",
        all_senders=False,
    )

    assert query == "{from:notice@bank.example} (newer_than:2y)"
    assert (
        compile_gmail_query(rules, custom_query="is:starred", all_senders=True)
        == "is:starred"
    )


def test_query_compiler_requires_an_explicit_scope_escape_hatch() -> None:
    with pytest.raises(SenderRuleConfigurationError, match="sender rules"):
        compile_gmail_query((), custom_query=None, all_senders=False)

    assert compile_gmail_query((), custom_query=None, all_senders=True) is None


@pytest.mark.parametrize(
    ("match_type", "value"),
    [
        (SenderMatchType.EXACT_ADDRESS, "from:attacker@example.com"),
        (SenderMatchType.EXACT_ADDRESS, "Name <notice@bank.example>"),
        (SenderMatchType.DOMAIN_SUFFIX, "bank.example OR newer_than:99y"),
        (SenderMatchType.DOMAIN_SUFFIX, "localhost"),
    ],
)
def test_sender_values_reject_query_fragments_and_unsafe_forms(
    match_type: SenderMatchType, value: str
) -> None:
    with pytest.raises(ValueError):
        normalize_sender_match(match_type, value)


def test_sender_values_and_institution_keys_normalize() -> None:
    assert (
        normalize_sender_match(SenderMatchType.EXACT_ADDRESS, "NOTICE@BANK.EXAMPLE")
        == "notice@bank.example"
    )
    assert (
        normalize_sender_match(SenderMatchType.DOMAIN_SUFFIX, "@BANK.EXAMPLE")
        == "bank.example"
    )
    assert validate_institution_key("FUBON_ALERTS") == "fubon_alerts"


def test_sender_rule_management_is_offline_and_account_scoped(
    migrated_database: Database,
) -> None:
    service = ManageSenderRules(
        lambda: SqlAlchemyUnitOfWork(migrated_database.session_factory)
    )
    with pytest.raises(SenderRuleConfigurationError, match="auth"):
        service.list_rules()
    with SqlAlchemyUnitOfWork(migrated_database.session_factory) as unit:
        unit.accounts.get_or_create("user@example.com")
        unit.commit()

    saved = service.add_or_update(
        institution_key="cathay",
        institution_name="國泰",
        match_type=SenderMatchType.EXACT_ADDRESS,
        match_value="notice@bank.example",
    )
    rules = service.list_rules()
    disabled = service.disable(saved.id)

    assert rules == (saved,)
    assert disabled.id == saved.id
    assert not disabled.enabled
