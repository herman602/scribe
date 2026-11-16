"""Read a versioned local JSON cutover file without contacting Google."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any

from app.domain.budget_enums import PaymentMethod, SalaryCalculationMode, WalletPurpose
from app.domain.cutover import (
    CutoverAccount,
    CutoverCategory,
    CutoverDocument,
    CutoverSalaryItem,
    CutoverSalaryTemplate,
    CutoverSubscription,
    CutoverTarget,
    CutoverWallet,
    LegacyDailyActivity,
    PendingSourceEvidence,
)


def _mapping(value: object, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _items(value: object, *, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError(f"{label} must be a list")
    return [_mapping(item, label=f"{label} item") for item in value]


def canonical_document_hash(payload: dict[str, Any]) -> str:
    """Hash semantic JSON content, unaffected by indentation or key order."""
    canonical = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(canonical).hexdigest()


def load_cutover_document(path: Path) -> tuple[CutoverDocument, str]:
    """Parse and validate one private normalized file."""
    payload = _mapping(json.loads(path.read_text(encoding="utf-8")), label="document")
    accounts = tuple(
        CutoverAccount(
            source_key=str(item["source_key"]),
            institution_code=str(item["institution_code"]),
            display_name=str(item["display_name"]),
            currency=str(item["currency"]),
            masked_reference=(
                None
                if item.get("masked_reference") is None
                else str(item["masked_reference"])
            ),
            balance_minor=int(item["balance_minor"]),
            wallets=tuple(
                CutoverWallet(
                    name=str(wallet["name"]),
                    balance_minor=int(wallet["balance_minor"]),
                    purpose=WalletPurpose(str(wallet.get("purpose", "general"))),
                )
                for wallet in _items(item.get("wallets", []), label="wallets")
            ),
        )
        for item in _items(payload.get("accounts", []), label="accounts")
    )
    categories = tuple(
        CutoverCategory(source_key=str(item["source_key"]), name=str(item["name"]))
        for item in _items(payload.get("categories", []), label="categories")
    )
    targets = tuple(
        CutoverTarget(
            source_key=str(item["source_key"]),
            year=int(item["year"]),
            month=int(item["month"]),
            currency=str(item["currency"]),
            category_key=str(item["category_key"]),
            target_minor=int(item["target_minor"]),
            buffer_minor=int(item["buffer_minor"]),
            funding_account_key=str(item["funding_account_key"]),
            funding_wallet_name=str(item["funding_wallet_name"]),
        )
        for item in _items(payload.get("targets", []), label="targets")
    )
    subscriptions = tuple(
        CutoverSubscription(
            source_key=str(item["source_key"]),
            name=str(item["name"]),
            amount_minor=int(item["amount_minor"]),
            category_key=str(item["category_key"]),
            funding_account_key=str(item["funding_account_key"]),
            funding_wallet_name=str(item["funding_wallet_name"]),
            effective_from=date.fromisoformat(str(item["effective_from"])),
        )
        for item in _items(payload.get("subscriptions", []), label="subscriptions")
    )
    salary_payload = payload.get("salary_template")
    salary_template = None
    if salary_payload is not None:
        salary = _mapping(salary_payload, label="salary_template")
        salary_template = CutoverSalaryTemplate(
            source_key=str(salary["source_key"]),
            name=str(salary["name"]),
            currency=str(salary["currency"]),
            source_account_key=str(salary["source_account_key"]),
            items=tuple(
                CutoverSalaryItem(
                    source_key=str(item["source_key"]),
                    name=str(item["name"]),
                    destination_account_key=str(item["destination_account_key"]),
                    destination_wallet_name=str(item["destination_wallet_name"]),
                    calculation_mode=SalaryCalculationMode(
                        str(item["calculation_mode"])
                    ),
                    fixed_amount_minor=(
                        None
                        if item.get("fixed_amount_minor") is None
                        else int(item["fixed_amount_minor"])
                    ),
                    category_key=(
                        None
                        if item.get("category_key") is None
                        else str(item["category_key"])
                    ),
                )
                for item in _items(salary.get("items", []), label="salary items")
            ),
        )
    legacy = tuple(
        LegacyDailyActivity(
            source_key=str(item["source_key"]),
            activity_date=date.fromisoformat(str(item["activity_date"])),
            sequence=int(item["sequence"]),
            amount_minor=int(item["amount_minor"]),
            currency=str(item["currency"]),
            payment_method=PaymentMethod(str(item["payment_method"])),
            note=None if item.get("note") is None else str(item["note"]),
        )
        for item in _items(payload.get("legacy_daily", []), label="legacy daily")
    )
    pending = tuple(
        PendingSourceEvidence(
            source_key=str(item["source_key"]),
            entity_kind=str(item["entity_kind"]),
            reason_code=str(item["reason_code"]),
            payload=_mapping(item.get("payload", {}), label="pending payload"),
        )
        for item in _items(payload.get("pending", []), label="pending")
    )
    document = CutoverDocument(
        format_version=int(payload["format_version"]),
        source_kind=str(payload["source_kind"]),
        source_locator_hash=str(payload["source_locator_hash"]),
        observed_at=datetime.fromisoformat(str(payload["observed_at"])),
        cutoff_at=datetime.fromisoformat(str(payload["cutoff_at"])),
        accounts=accounts,
        categories=categories,
        targets=targets,
        subscriptions=subscriptions,
        salary_template=salary_template,
        legacy_daily=legacy,
        pending=pending,
    )
    return document, canonical_document_hash(payload)
