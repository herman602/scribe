"""Framework-free Gmail authentication, transport, and decoded message types."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_READONLY_SCOPES = (GMAIL_READONLY_SCOPE,)
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_MODIFY_SCOPES = (GMAIL_MODIFY_SCOPE,)


def gmail_oauth_scopes(*, labeling_enabled: bool) -> tuple[str, ...]:
    """Select the exact least-privilege scope for configured behavior."""
    return GMAIL_MODIFY_SCOPES if labeling_enabled else GMAIL_READONLY_SCOPES


class TokenState(StrEnum):
    """Local OAuth token readiness without exposing token material."""

    MISSING = "missing"
    VALID = "valid"
    EXPIRED_REFRESHABLE = "expired_refreshable"
    INVALID = "invalid"


class GmailAuthorizationState(StrEnum):
    """Durable readiness of the mailbox authorization."""

    HEALTHY = "healthy"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


class GmailAuthorizationNoticeKind(StrEnum):
    """Stable reasons the dashboard should call attention to Gmail OAuth."""

    TESTING_EXPIRES_TOMORROW = "testing_expires_tomorrow"
    TESTING_EXPIRED = "testing_expired"
    REAUTHORIZATION_REQUIRED = "reauthorization_required"


@dataclass(frozen=True)
class TokenStatus:
    """Non-sensitive OAuth token diagnostic."""

    state: TokenState
    expires_at: datetime | None = None
    detail: str | None = None


@dataclass(frozen=True)
class GmailAuthorizationHealth:
    """Non-secret authorization health persisted in local settings."""

    state: GmailAuthorizationState
    authorized_at: datetime | None
    failure_at: datetime | None
    failure_reason_code: str | None


@dataclass(frozen=True)
class GmailAuthorizationNotice:
    """One stable dashboard notice derived from authorization health."""

    kind: GmailAuthorizationNoticeKind
    authorized_at: datetime | None
    due_at: datetime | None
    failure_at: datetime | None


@dataclass(frozen=True)
class GmailAccountProfile:
    """Authenticated Gmail mailbox identity and current counters."""

    email_address: str
    messages_total: int
    threads_total: int
    history_id: str


@dataclass(frozen=True)
class GmailMessageRef:
    """A message-level list result; thread IDs never replace message identity."""

    message_id: str
    thread_id: str


@dataclass(frozen=True)
class GmailMessagePage:
    """One page returned by ``users.messages.list``."""

    messages: tuple[GmailMessageRef, ...]
    next_page_token: str | None
    result_size_estimate: int | None


@dataclass(frozen=True)
class GmailHistoryChange:
    """One message-level change reported by Gmail mailbox history."""

    message_id: str
    thread_id: str
    message_added: bool = False
    message_deleted: bool = False
    labels_added: tuple[str, ...] = ()
    labels_removed: tuple[str, ...] = ()


@dataclass(frozen=True)
class GmailHistoryPage:
    """One durable page returned by ``users.history.list``."""

    changes: tuple[GmailHistoryChange, ...]
    next_page_token: str | None
    latest_history_id: str


@dataclass(frozen=True)
class GmailMessageMetadata:
    """Minimal message values used by incremental local sender gating."""

    message_id: str
    thread_id: str
    history_id: str
    sender_header: str
    label_ids: tuple[str, ...]


@dataclass(frozen=True)
class GmailHeader:
    """One MIME header preserved in source order."""

    name: str
    value: str


@dataclass(frozen=True)
class GmailMessagePart:
    """Recursive Gmail ``MessagePart`` without Google client types."""

    part_id: str
    mime_type: str
    filename: str
    headers: tuple[GmailHeader, ...]
    body_size: int
    body_data: bytes | None
    attachment_id: str | None
    parts: tuple[GmailMessagePart, ...] = ()


@dataclass(frozen=True)
class GmailRawMessage:
    """A complete Gmail message payload ready for local MIME decoding."""

    message_id: str
    thread_id: str
    history_id: str
    internal_date_ms: int
    snippet: str
    label_ids: tuple[str, ...]
    payload: GmailMessagePart


@dataclass(frozen=True)
class GmailThreadMessage:
    """Message metadata returned while expanding a Gmail thread."""

    message_id: str
    thread_id: str
    sender_header: str


@dataclass(frozen=True)
class GmailThread:
    """All message entries in one relevant Gmail thread."""

    thread_id: str
    messages: tuple[GmailThreadMessage, ...]


@dataclass(frozen=True)
class GmailAttachmentBytes:
    """Decoded bytes returned by ``users.messages.attachments.get``."""

    data: bytes


@dataclass(frozen=True)
class DecodeWarning:
    """Non-sensitive MIME recovery detail."""

    code: str
    detail: str


@dataclass(frozen=True)
class DecodedAttachment:
    """Ephemeral attachment content plus persistence-safe metadata."""

    mime_part_id: str
    filename: str
    mime_type: str
    gmail_attachment_id: str | None
    content: bytes


@dataclass(frozen=True)
class DecodedEmail:
    """Locally decoded email values with no Google client dependency."""

    message_id: str
    thread_id: str
    history_id: str
    internal_date_ms: int
    sender: str
    recipients: tuple[str, ...]
    subject: str
    received_at: datetime | None
    snippet: str
    text_body: str
    has_html_body: bool
    label_ids: tuple[str, ...]
    attachments: tuple[DecodedAttachment, ...]
    warnings: tuple[DecodeWarning, ...]


type AttachmentLoader = Callable[[str, str], GmailAttachmentBytes]


class MimeDecoder(Protocol):
    """Decode one normalized Gmail MIME tree entirely locally."""

    def decode(
        self, message: GmailRawMessage, attachment_loader: AttachmentLoader
    ) -> DecodedEmail: ...


class GmailAuthPort(Protocol):
    """Application-facing Gmail authorization boundary."""

    def authorize(
        self, *, force_reauthorization: bool = False
    ) -> GmailAccountProfile: ...

    def token_status(self) -> TokenStatus: ...


class GmailProfileGateway(Protocol):
    """Minimal profile boundary needed by OAuth and diagnostics."""

    def get_profile(self) -> GmailAccountProfile: ...


class GmailFullSyncGateway(Protocol):
    """Application-facing Gmail boundary needed by full synchronization."""

    def get_profile(self) -> GmailAccountProfile: ...

    def list_messages(
        self,
        *,
        query: str | None,
        page_token: str | None,
        max_results: int,
        include_spam_trash: bool,
    ) -> GmailMessagePage: ...

    def get_thread(self, thread_id: str) -> GmailThread: ...

    def get_message(self, message_id: str) -> GmailRawMessage: ...

    def get_attachment(
        self, message_id: str, attachment_id: str
    ) -> GmailAttachmentBytes: ...


class GmailGateway(GmailFullSyncGateway, Protocol):
    """Complete Gmail boundary including mailbox history synchronization."""

    def list_history(
        self,
        *,
        start_history_id: str,
        page_token: str | None,
        max_results: int,
    ) -> GmailHistoryPage: ...

    def get_message_metadata(self, message_id: str) -> GmailMessageMetadata: ...


class GmailLabelGateway(Protocol):
    """Narrow write boundary limited to one user Label and message IDs."""

    def ensure_label(self, label_name: str) -> str: ...

    def add_label_to_messages(
        self, message_ids: tuple[str, ...], label_id: str
    ) -> None: ...
