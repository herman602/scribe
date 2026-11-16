"""Gmail authorization use case."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from app.application.services.gmail_auth_health import GmailAuthHealthService
from app.domain.gmail import GmailAccountProfile, GmailAuthPort
from app.domain.models import GmailAccount
from app.domain.ports.repositories import UnitOfWork

type UnitOfWorkFactory = Callable[[], UnitOfWork]


@dataclass(frozen=True)
class GmailAuthenticationResult:
    """Authenticated mailbox and local account identity."""

    profile: GmailAccountProfile
    account: GmailAccount


class AuthenticateGmail:
    """Authorize Gmail and persist only its normalized profile identity."""

    def __init__(
        self,
        auth: GmailAuthPort,
        unit_of_work_factory: UnitOfWorkFactory,
        health: GmailAuthHealthService,
    ) -> None:
        self._auth = auth
        self._unit_of_work_factory = unit_of_work_factory
        self._health = health

    def execute(self) -> GmailAuthenticationResult:
        """Complete OAuth/profile verification and persist the account."""
        # `app auth` must obtain a new grant rather than merely reuse a valid
        # token; otherwise a Testing-mode reminder could be cleared without
        # extending Google's refresh-token lifetime.
        profile = self._auth.authorize(force_reauthorization=True)
        with self._unit_of_work_factory() as unit:
            account = unit.accounts.get_or_create(profile.email_address)
            unit.commit()
        self._health.mark_authorized()
        return GmailAuthenticationResult(profile=profile, account=account)
