"""Pure validation, matching, and Gmail-query compilation for sender rules."""

from __future__ import annotations

import re
from collections.abc import Sequence
from email.utils import parseaddr

from app.domain.enums import SenderMatchType
from app.domain.errors import SenderRuleConfigurationError
from app.domain.models import SenderRule

_INSTITUTION_KEY = re.compile(r"^[a-z0-9][a-z0-9_-]{0,99}$")
_DOMAIN = re.compile(
    r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def validate_institution_key(value: str) -> str:
    """Return a stable lowercase institution key."""
    normalized = value.strip().lower()
    if not _INSTITUTION_KEY.fullmatch(normalized):
        raise ValueError(
            "institution key must use lowercase letters, digits, '-' or '_'"
        )
    return normalized


def normalize_sender_match(match_type: SenderMatchType, value: str) -> str:
    """Normalize a safe exact address or domain; Gmail syntax is not accepted."""
    candidate = value.strip().lower()
    if match_type is SenderMatchType.EXACT_ADDRESS:
        _, address = parseaddr(candidate)
        if (
            address != candidate
            or address.count("@") != 1
            or any(char.isspace() for char in address)
        ):
            raise ValueError("exact sender must be one plain email address")
        local, domain = address.rsplit("@", 1)
        if not local or not _DOMAIN.fullmatch(domain):
            raise ValueError("exact sender email address is invalid")
        return f"{local}@{domain}"

    domain = candidate.removeprefix("@")
    if not _DOMAIN.fullmatch(domain):
        raise ValueError("sender domain is invalid")
    return domain


def sender_address(value: str) -> str | None:
    """Extract and normalize the mailbox from an RFC From header."""
    _, address = parseaddr(value)
    normalized = address.strip().lower()
    if normalized.count("@") != 1:
        return None
    return normalized


def match_sender(from_header: str, rules: Sequence[SenderRule]) -> SenderRule | None:
    """Return the first priority-ordered rule matching one From address."""
    address = sender_address(from_header)
    if address is None:
        return None
    _, domain = address.rsplit("@", 1)
    for rule in sorted(rules, key=lambda item: (item.priority, item.id)):
        if not rule.enabled:
            continue
        if (
            rule.match_type is SenderMatchType.EXACT_ADDRESS
            and address == rule.match_value_normalized
        ):
            return rule
        if rule.match_type is SenderMatchType.DOMAIN_SUFFIX and (
            domain == rule.match_value_normalized
            or domain.endswith(f".{rule.match_value_normalized}")
        ):
            return rule
    return None


def compile_gmail_query(
    rules: Sequence[SenderRule],
    *,
    custom_query: str | None,
    all_senders: bool,
) -> str | None:
    """Compile validated rule values and an optional user query."""
    custom = custom_query.strip() if custom_query else None
    if all_senders:
        return custom

    discovery_rules = [
        rule for rule in rules if rule.enabled and rule.gmail_query_enabled
    ]
    if not discovery_rules:
        raise SenderRuleConfigurationError(
            "No Gmail-query-enabled sender rules exist. Add a sender rule or "
            "use --all-senders explicitly."
        )
    clauses = []
    for rule in discovery_rules:
        value = rule.match_value_normalized
        if rule.match_type is SenderMatchType.DOMAIN_SUFFIX:
            value = f"@{value}"
        clauses.append(f"from:{value}")
    sender_query = "{" + " ".join(sorted(set(clauses))) + "}"
    return f"{sender_query} ({custom})" if custom else sender_query
