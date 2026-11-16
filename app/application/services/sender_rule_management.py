"""Use cases for offline sender-rule administration."""

from __future__ import annotations

from collections.abc import Callable
from uuid import uuid4

from app.domain.enums import SenderMatchType
from app.domain.errors import SenderRuleConfigurationError
from app.domain.models import GmailAccount, SenderRule, SenderRuleDraft
from app.domain.parsing import SecretStore, SecretValue
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]


class ManageSenderRules:
    """Manage flexible bank sender rules without contacting Gmail."""

    def __init__(
        self,
        unit_of_work_factory: UnitOfWorkFactory,
        secret_store: SecretStore | None = None,
    ) -> None:
        self._unit_of_work_factory = unit_of_work_factory
        self._secret_store = secret_store

    def list_rules(self, account_email: str | None = None) -> tuple[SenderRule, ...]:
        """List all rules for one unambiguous local Gmail account."""
        with self._unit_of_work_factory() as unit:
            account = _resolve_account(unit, account_email)
            return unit.sender_rules.list_all(account.id)

    def add_or_update(
        self,
        *,
        institution_key: str,
        institution_name: str,
        match_type: SenderMatchType,
        match_value: str,
        account_email: str | None = None,
        parser_name: str | None = None,
        priority: int = 100,
        gmail_query_enabled: bool = True,
    ) -> SenderRule:
        """Add or update a unique exact-address/domain rule."""
        with self._unit_of_work_factory() as unit:
            account = _resolve_account(unit, account_email)
            rule = unit.sender_rules.upsert(
                SenderRuleDraft(
                    gmail_account_id=account.id,
                    institution_key=institution_key,
                    institution_name=institution_name,
                    match_type=match_type,
                    match_value=match_value,
                    parser_name=parser_name,
                    priority=priority,
                    gmail_query_enabled=gmail_query_enabled,
                )
            )
            unit.commit()
            return rule

    def disable(self, rule_id: int, *, account_email: str | None = None) -> SenderRule:
        """Disable one rule after checking account ownership."""
        with self._unit_of_work_factory() as unit:
            account = _resolve_account(unit, account_email)
            rules = unit.sender_rules.list_all(account.id)
            if rule_id not in {rule.id for rule in rules}:
                raise SenderRuleConfigurationError(
                    f"Sender rule {rule_id} does not belong to {account.profile_email}"
                )
            rule = unit.sender_rules.disable(rule_id)
            if rule is None:  # pragma: no cover - protected by same transaction
                raise SenderRuleConfigurationError(
                    f"Sender rule {rule_id} no longer exists"
                )
            unit.commit()
            return rule

    def add_pdf_password(
        self,
        rule_id: int,
        password: SecretValue,
        *,
        account_email: str | None = None,
    ) -> SenderRule:
        """Store a password in keyring and only its opaque reference in SQLite."""
        secret_store = self._require_secret_store()
        with self._unit_of_work_factory() as unit:
            account = _resolve_account(unit, account_email)
            rule = unit.sender_rules.get(rule_id)
            if rule is None or rule.gmail_account_id != account.id:
                raise SenderRuleConfigurationError(
                    f"Sender rule {rule_id} does not belong to {account.profile_email}"
                )
            reference = f"sender-rule-{rule.id}-{uuid4()}"
            secret_store.set(reference, password)
            try:
                updated = unit.sender_rules.set_pdf_password_refs(
                    rule.id, (*rule.pdf_password_refs, reference)
                )
                if updated is None:  # pragma: no cover - same transaction
                    raise SenderRuleConfigurationError(
                        f"Sender rule {rule_id} no longer exists"
                    )
                unit.commit()
            except Exception:
                secret_store.delete(reference)
                raise
            return updated

    def clear_pdf_passwords(
        self, rule_id: int, *, account_email: str | None = None
    ) -> SenderRule:
        """Remove configured PDF passwords from keyring and sender rule."""
        secret_store = self._require_secret_store()
        with self._unit_of_work_factory() as unit:
            account = _resolve_account(unit, account_email)
            rule = unit.sender_rules.get(rule_id)
            if rule is None or rule.gmail_account_id != account.id:
                raise SenderRuleConfigurationError(
                    f"Sender rule {rule_id} does not belong to {account.profile_email}"
                )
            for reference in rule.pdf_password_refs:
                secret_store.delete(reference)
            updated = unit.sender_rules.set_pdf_password_refs(rule.id, ())
            if updated is None:  # pragma: no cover - same transaction
                raise SenderRuleConfigurationError(
                    f"Sender rule {rule_id} no longer exists"
                )
            unit.commit()
            return updated

    def _require_secret_store(self) -> SecretStore:
        if self._secret_store is None:
            raise SenderRuleConfigurationError(
                "No OS credential-store adapter is configured"
            )
        return self._secret_store


def _resolve_account(unit: UnitOfWork, account_email: str | None) -> GmailAccount:
    if account_email:
        account = unit.accounts.get_by_profile_email(account_email)
        if account is None:
            raise SenderRuleConfigurationError(
                f"No authorized local Gmail account matches {account_email}"
            )
        return account
    accounts = unit.accounts.list_all()
    if not accounts:
        raise SenderRuleConfigurationError(
            "No local Gmail account exists; run `uv run python -m app auth` first"
        )
    if len(accounts) > 1:
        raise SenderRuleConfigurationError(
            "More than one Gmail account exists; pass --account"
        )
    return accounts[0]
