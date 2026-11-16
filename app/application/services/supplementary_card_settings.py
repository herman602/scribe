"""Record a card's holder role from one transaction that names it.

The holder role belongs to the card and nowhere else (ADR-B101). This
command exists because a supplementary card is usually revealed by a
notification long before anyone decides where it settles, so it must be
possible to record the role without first completing the card's
configuration.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.domain.budget_enums import CardHolderRole
from app.domain.budget_models import CardConfigurationDraft, CardDraft, CardIdentity
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class SetSupplementaryCardRequest:
    """User intent to mark or unmark the card identified by one transaction."""

    transaction_id: int
    supplementary: bool


@dataclass(frozen=True)
class SetSupplementaryCardResult:
    """Resolved identity and the holder role now stored on the card."""

    institution_key: str
    institution_name: str
    card_last_four: str
    supplementary: bool
    card_id: int
    card_created: bool


class SetSupplementaryCard:
    """Resolve a transaction to a card, then store that card's holder role."""

    def __init__(self, unit_of_work_factory: UnitOfWorkFactory) -> None:
        self._unit_of_work_factory = unit_of_work_factory

    def execute(
        self, request: SetSupplementaryCardRequest
    ) -> SetSupplementaryCardResult:
        """Write the role onto the card, creating the identity if it is new."""
        if request.transaction_id < 1:
            raise ValueError("transaction ID must be positive")

        role = (
            CardHolderRole.SUPPLEMENTARY
            if request.supplementary
            else CardHolderRole.PRIMARY
        )
        with self._unit_of_work_factory() as unit:
            transaction = unit.transactions.get(request.transaction_id)
            if transaction is None:
                raise ValueError("transaction was not found")
            if transaction.card_last_four is None:
                raise ValueError("transaction has no card last four")

            email = unit.emails.get(transaction.email_id)
            if email is None:
                raise ValueError("source email was not found")
            if email.matched_sender_rule_id is None:
                raise ValueError("source email is not linked to a bank sender rule")

            sender_rule = unit.sender_rules.get(email.matched_sender_rule_id)
            if sender_rule is None:
                raise ValueError("bank sender rule was not found")

            identity = CardIdentity(
                gmail_account_id=email.gmail_account_id,
                issuer_code=sender_rule.institution_key,
                last_four=transaction.card_last_four,
            )
            existing = unit.cards.get_by_identity(identity)
            if existing is None:
                card_id = unit.cards.add(
                    CardDraft(
                        gmail_account_id=identity.gmail_account_id,
                        issuer_code=identity.issuer_code,
                        display_name=(
                            f"{sender_rule.institution_name} ****{identity.last_four}"
                        ),
                        last_four=identity.last_four,
                        holder_role=role,
                    )
                ).id
                created = True
            else:
                # Only the role is being decided here, so every settlement
                # value the user configured is carried through unchanged.
                card_id = unit.cards.save_configuration(
                    CardConfigurationDraft(
                        card_id=existing.id,
                        expected_version=existing.version,
                        display_name=existing.display_name,
                        holder_role=role,
                        liability_account_id=existing.liability_account_id,
                        settlement_account_id=existing.settlement_account_id,
                        default_funding_wallet_id=existing.default_funding_wallet_id,
                    )
                ).id
                created = False
            unit.commit()
            return SetSupplementaryCardResult(
                institution_key=sender_rule.institution_key,
                institution_name=sender_rule.institution_name,
                card_last_four=transaction.card_last_four,
                supplementary=request.supplementary,
                card_id=card_id,
                card_created=created,
            )
