"""Deterministic, auditable matching between two money observations.

Scoring is intentionally boring: amount and currency are gates that add no
points, structural agreement adds points, and merchant text can only support a
match that already agrees on everything else. No fuzzy-matching dependency is
used, and no reason ever carries merchant evidence or Email content.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.domain.budget_enums import (
    FinancialEventType,
    SourceLinkReason,
    SourceLinkStatus,
)
from app.domain.budget_models import SourceLinkDraft

MAX_SCORE_BPS = 10000
AUTO_LINK_SCORE_BPS = 10000
EXACT_CARD_BPS = 3000
COMPATIBLE_EVENT_TYPE_BPS = 2000
TIME_WITHIN_24H_BPS = 2500
TIME_WITHIN_72H_BPS = 1000
EXACT_MERCHANT_BPS = 2500
TOKEN_MERCHANT_BPS = 1500

CLOSE_TIME_WINDOW = timedelta(hours=24)
MATCH_TIME_WINDOW = timedelta(hours=72)

_PUNCTUATION = re.compile(r"[^\w\s]", re.UNICODE)
_WHITESPACE = re.compile(r"\s+")


def normalize_merchant(value: str | None) -> str:
    """Return an NFKC, case-folded, punctuation-free, collapsed merchant key."""
    if value is None:
        return ""
    normalized = unicodedata.normalize("NFKC", value)
    normalized = _PUNCTUATION.sub(" ", normalized).casefold()
    return _WHITESPACE.sub(" ", normalized).strip()


def merchant_tokens(value: str | None) -> frozenset[str]:
    """Return the normalized token set used for conservative comparison."""
    normalized = normalize_merchant(value)
    return frozenset(normalized.split()) if normalized else frozenset()


@dataclass(frozen=True)
class MatchObservation:
    """One side of a comparison, expressed without any storage identity."""

    amount_minor: int
    currency: str
    event_type: FinancialEventType
    occurred_at: datetime
    card_id: int | None = None
    merchant: str | None = None


@dataclass(frozen=True)
class MatchScore:
    """Deterministic comparison result with stable, non-sensitive reasons."""

    eligible: bool
    score_bps: int
    reasons: tuple[SourceLinkReason, ...]

    @property
    def meets_auto_link_threshold(self) -> bool:
        """Return whether a single candidate could ever be linked without review.

        B2 never acts on this: intake persists candidates only. The automatic
        link decision belongs to the later manual-matching milestone.
        """
        return self.eligible and self.score_bps >= AUTO_LINK_SCORE_BPS


_UNELIGIBLE = MatchScore(eligible=False, score_bps=0, reasons=())


def score_match(source: MatchObservation, candidate: MatchObservation) -> MatchScore:
    """Compare two observations using mandatory gates plus bounded evidence."""
    if source.amount_minor != candidate.amount_minor:
        return _UNELIGIBLE
    if source.currency != candidate.currency:
        return _UNELIGIBLE
    if source.event_type is not candidate.event_type:
        return _UNELIGIBLE
    if (
        source.card_id is not None
        and candidate.card_id is not None
        and source.card_id != candidate.card_id
    ):
        return _UNELIGIBLE
    distance = abs(source.occurred_at - candidate.occurred_at)
    if distance > MATCH_TIME_WINDOW:
        return _UNELIGIBLE

    reasons: list[SourceLinkReason] = [SourceLinkReason.COMPATIBLE_EVENT_TYPE]
    score = COMPATIBLE_EVENT_TYPE_BPS
    if source.card_id is not None and candidate.card_id is not None:
        reasons.append(SourceLinkReason.EXACT_CARD)
        score += EXACT_CARD_BPS
    if distance <= CLOSE_TIME_WINDOW:
        reasons.append(SourceLinkReason.TIME_WITHIN_24H)
        score += TIME_WITHIN_24H_BPS
    else:
        reasons.append(SourceLinkReason.TIME_WITHIN_72H)
        score += TIME_WITHIN_72H_BPS
    score += _merchant_score(source.merchant, candidate.merchant, reasons)
    return MatchScore(eligible=True, score_bps=score, reasons=tuple(reasons))


def _merchant_score(
    source: str | None, candidate: str | None, reasons: list[SourceLinkReason]
) -> int:
    """Add supporting merchant evidence; it can never link on its own."""
    left = normalize_merchant(source)
    right = normalize_merchant(candidate)
    if not left or not right:
        return 0
    if left == right:
        reasons.append(SourceLinkReason.EXACT_MERCHANT)
        return EXACT_MERCHANT_BPS
    left_tokens = merchant_tokens(source)
    right_tokens = merchant_tokens(candidate)
    if left_tokens <= right_tokens or right_tokens <= left_tokens:
        reasons.append(SourceLinkReason.TOKEN_MERCHANT)
        return TOKEN_MERCHANT_BPS
    return 0


def candidate_link_draft(
    *, financial_event_id: int, transaction_id: int, score: MatchScore
) -> SourceLinkDraft:
    """Turn a score into a link draft that is always awaiting review.

    A scored comparison can never promote itself to ``confirmed``. Only an
    explicit provenance decision, such as direct Gmail intake or a later user
    confirmation, may create a confirmed link.
    """
    if not score.eligible:
        raise ValueError("an ineligible comparison cannot become a candidate")
    return SourceLinkDraft(
        financial_event_id=financial_event_id,
        transaction_id=transaction_id,
        status=SourceLinkStatus.CANDIDATE,
        match_score_bps=score.score_bps,
        reasons=score.reasons,
    )
