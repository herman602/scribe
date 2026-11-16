"""Gmail API adapter with typed payloads and bounded retry/backoff."""

from __future__ import annotations

import base64
import binascii
import json
import logging
import random
import time
from collections.abc import Callable
from email.utils import parsedate_to_datetime
from typing import Any, Protocol, cast

import httplib2  # type: ignore[import-untyped]
from google.oauth2.credentials import Credentials
from google_auth_httplib2 import AuthorizedHttp  # type: ignore[import-untyped]
from googleapiclient.discovery import build  # type: ignore[import-untyped]
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]

from app.domain.errors import (
    GmailAuthenticationError,
    GmailConnectionError,
    GmailHistoryExpiredError,
    GmailLabelError,
    GmailMessageFetchError,
    GmailMessageNotFoundError,
    GmailRateLimitError,
    ScribeError,
)
from app.domain.gmail import (
    GmailAccountProfile,
    GmailAttachmentBytes,
    GmailHeader,
    GmailHistoryChange,
    GmailHistoryPage,
    GmailMessageMetadata,
    GmailMessagePage,
    GmailMessagePart,
    GmailMessageRef,
    GmailRawMessage,
    GmailThread,
    GmailThreadMessage,
)
from app.infrastructure.observability.logging import LOGGER_NAME

_RATE_LIMIT_REASONS = {
    "dailyLimitExceeded",
    "rateLimitExceeded",
    "userRateLimitExceeded",
    "quotaExceeded",
}
_TRANSIENT_HTTP_STATUS = {500, 502, 503, 504}
_LOGGER = logging.getLogger(LOGGER_NAME)


class _ExecutableRequest(Protocol):
    def execute(self) -> Any: ...


class _AttachmentsResource(Protocol):
    def get(self, *, userId: str, messageId: str, id: str) -> _ExecutableRequest: ...


class _MessagesResource(Protocol):
    def list(self, **kwargs: Any) -> _ExecutableRequest: ...

    def get(self, **kwargs: Any) -> _ExecutableRequest: ...

    def batchModify(self, **kwargs: Any) -> _ExecutableRequest: ...

    def attachments(self) -> _AttachmentsResource: ...


class _ThreadsResource(Protocol):
    def get(self, **kwargs: Any) -> _ExecutableRequest: ...


class _HistoryResource(Protocol):
    def list(self, **kwargs: Any) -> _ExecutableRequest: ...


class _LabelsResource(Protocol):
    def list(self, **kwargs: Any) -> _ExecutableRequest: ...

    def create(self, **kwargs: Any) -> _ExecutableRequest: ...


class _UsersResource(Protocol):
    def getProfile(self, *, userId: str) -> _ExecutableRequest: ...

    def messages(self) -> _MessagesResource: ...

    def threads(self) -> _ThreadsResource: ...

    def history(self) -> _HistoryResource: ...

    def labels(self) -> _LabelsResource: ...


class _GmailService(Protocol):
    def users(self) -> _UsersResource: ...


type GmailServiceFactory = Callable[[Credentials], Any]
type Sleeper = Callable[[float], None]
type RandomSource = Callable[[], float]


def _build_service(
    credentials: Credentials,
    request_timeout_seconds: int,
) -> _GmailService:
    authorized_http = AuthorizedHttp(
        credentials,
        http=httplib2.Http(timeout=request_timeout_seconds),
    )
    return cast(
        _GmailService,
        build(
            "gmail",
            "v1",
            http=authorized_http,
            cache_discovery=False,
        ),
    )


class GoogleGmailGateway:
    """Translate the dynamic Google client into safe domain records."""

    def __init__(
        self,
        credentials: Credentials,
        *,
        request_timeout_seconds: int = 30,
        max_attempts: int = 5,
        backoff_base_seconds: float = 1.0,
        backoff_max_seconds: float = 64.0,
        service_factory: GmailServiceFactory | None = None,
        sleeper: Sleeper = time.sleep,
        random_source: RandomSource = random.random,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("Gmail max attempts must be positive")
        if backoff_base_seconds < 1 or backoff_max_seconds < backoff_base_seconds:
            raise ValueError("Gmail backoff bounds are invalid")
        self._credentials = credentials
        self._service_factory: GmailServiceFactory = service_factory or (
            lambda current: _build_service(current, request_timeout_seconds)
        )
        self._max_attempts = max_attempts
        self._backoff_base_seconds = backoff_base_seconds
        self._backoff_max_seconds = backoff_max_seconds
        self._sleeper = sleeper
        self._random_source = random_source
        self._service_instance: _GmailService | None = None

    def get_profile(self) -> GmailAccountProfile:
        """Return the authenticated user's mailbox profile."""
        response = self._execute(
            lambda: self._service().users().getProfile(userId="me"),
            operation="profile",
        )
        payload = _mapping(response, "Gmail profile")
        try:
            email_address = str(payload["emailAddress"]).strip().lower()
            messages_total = int(payload["messagesTotal"])
            threads_total = int(payload["threadsTotal"])
            history_id = str(payload["historyId"])
        except (KeyError, TypeError, ValueError) as error:
            raise GmailConnectionError(
                "Gmail profile response was incomplete"
            ) from error
        if (
            not email_address
            or not history_id
            or messages_total < 0
            or threads_total < 0
        ):
            raise GmailConnectionError("Gmail profile response had invalid values")
        return GmailAccountProfile(
            email_address=email_address,
            messages_total=messages_total,
            threads_total=threads_total,
            history_id=history_id,
        )

    def list_messages(
        self,
        *,
        query: str | None,
        page_token: str | None,
        max_results: int,
        include_spam_trash: bool,
    ) -> GmailMessagePage:
        """Return one message-level page without fetching message bodies."""
        if not 1 <= max_results <= 500:
            raise ValueError("Gmail max_results must be between 1 and 500")
        arguments: dict[str, Any] = {
            "userId": "me",
            "maxResults": max_results,
            "includeSpamTrash": include_spam_trash,
        }
        if query:
            arguments["q"] = query
        if page_token:
            arguments["pageToken"] = page_token
        response = self._execute(
            lambda: self._service().users().messages().list(**arguments),
            operation="messages.list",
        )
        payload = _mapping(response, "Gmail message list")
        raw_messages = payload.get("messages", [])
        if not isinstance(raw_messages, list):
            raise GmailMessageFetchError("Gmail message list was invalid")
        messages: list[GmailMessageRef] = []
        for raw in raw_messages:
            item = _mapping(raw, "Gmail message reference")
            message_id = str(item.get("id", ""))
            thread_id = str(item.get("threadId", ""))
            if not message_id or not thread_id:
                raise GmailMessageFetchError("Gmail message reference was incomplete")
            messages.append(GmailMessageRef(message_id, thread_id))
        estimate_value = payload.get("resultSizeEstimate")
        try:
            estimate = None if estimate_value is None else int(estimate_value)
        except (TypeError, ValueError) as error:
            raise GmailMessageFetchError(
                "Gmail result size estimate was invalid"
            ) from error
        token = payload.get("nextPageToken")
        return GmailMessagePage(
            messages=tuple(messages),
            next_page_token=str(token) if token else None,
            result_size_estimate=estimate,
        )

    def get_thread(self, thread_id: str) -> GmailThread:
        """Expand a relevant thread and return every message's From header."""
        response = self._execute(
            lambda: (
                self._service()
                .users()
                .threads()
                .get(
                    userId="me",
                    id=thread_id,
                    format="metadata",
                    metadataHeaders=["From"],
                )
            ),
            operation="threads.get",
        )
        payload = _mapping(response, "Gmail thread")
        returned_id = str(payload.get("id", ""))
        raw_messages = payload.get("messages")
        if returned_id != thread_id or not isinstance(raw_messages, list):
            raise GmailMessageFetchError("Gmail thread response was incomplete")
        messages = []
        for raw in raw_messages:
            item = _mapping(raw, "Gmail thread message")
            message_id = str(item.get("id", ""))
            message_thread_id = str(item.get("threadId", ""))
            message_payload = _mapping(item.get("payload", {}), "message metadata")
            headers = _parse_headers(message_payload.get("headers", []))
            sender = next(
                (
                    header.value
                    for header in headers
                    if header.name.casefold() == "from"
                ),
                "",
            )
            if not message_id or message_thread_id != thread_id:
                raise GmailMessageFetchError(
                    "Gmail thread message identity was incomplete"
                )
            messages.append(
                GmailThreadMessage(
                    message_id=message_id,
                    thread_id=message_thread_id,
                    sender_header=sender,
                )
            )
        return GmailThread(thread_id=thread_id, messages=tuple(messages))

    def list_history(
        self,
        *,
        start_history_id: str,
        page_token: str | None,
        max_results: int,
    ) -> GmailHistoryPage:
        """Return one mailbox-wide page of message-level history changes."""
        if not start_history_id:
            raise ValueError("Gmail start_history_id cannot be empty")
        if not 1 <= max_results <= 500:
            raise ValueError("Gmail max_results must be between 1 and 500")
        arguments: dict[str, Any] = {
            "userId": "me",
            "startHistoryId": start_history_id,
            "maxResults": max_results,
            "historyTypes": [
                "messageAdded",
                "messageDeleted",
                "labelAdded",
                "labelRemoved",
            ],
        }
        if page_token:
            arguments["pageToken"] = page_token
        response = self._execute(
            lambda: self._service().users().history().list(**arguments),
            operation="history.list",
        )
        payload = _mapping(response, "Gmail history list")
        latest_history_id = str(payload.get("historyId", ""))
        if not latest_history_id:
            raise GmailMessageFetchError("Gmail history response had no history ID")
        raw_history = payload.get("history", [])
        if not isinstance(raw_history, list):
            raise GmailMessageFetchError("Gmail history records were invalid")
        changes = tuple(
            change
            for raw_record in raw_history
            for change in _parse_history_record(raw_record)
        )
        token = payload.get("nextPageToken")
        return GmailHistoryPage(
            changes=changes,
            next_page_token=str(token) if token else None,
            latest_history_id=latest_history_id,
        )

    def get_message_metadata(self, message_id: str) -> GmailMessageMetadata:
        """Fetch only sender and labels before accepting an incremental item."""
        response = self._execute(
            lambda: (
                self._service()
                .users()
                .messages()
                .get(
                    userId="me",
                    id=message_id,
                    format="metadata",
                    metadataHeaders=["From"],
                )
            ),
            operation="messages.get.metadata",
        )
        payload = _mapping(response, "Gmail message metadata")
        returned_id = str(payload.get("id", ""))
        thread_id = str(payload.get("threadId", ""))
        history_id = str(payload.get("historyId", ""))
        label_ids = payload.get("labelIds", [])
        message_payload = _mapping(payload.get("payload", {}), "message metadata")
        headers = _parse_headers(message_payload.get("headers", []))
        sender = next(
            (header.value for header in headers if header.name.casefold() == "from"),
            "",
        )
        if (
            returned_id != message_id
            or not thread_id
            or not history_id
            or not isinstance(label_ids, list)
        ):
            raise GmailMessageFetchError("Gmail message metadata was incomplete")
        return GmailMessageMetadata(
            message_id=returned_id,
            thread_id=thread_id,
            history_id=history_id,
            sender_header=sender,
            label_ids=tuple(str(item) for item in label_ids),
        )

    def get_message(self, message_id: str) -> GmailRawMessage:
        """Fetch and normalize one complete Gmail MIME payload."""
        response = self._execute(
            lambda: (
                self._service()
                .users()
                .messages()
                .get(userId="me", id=message_id, format="full")
            ),
            operation="messages.get",
        )
        payload = _mapping(response, "Gmail message")
        returned_id = str(payload.get("id", ""))
        thread_id = str(payload.get("threadId", ""))
        history_id = str(payload.get("historyId", ""))
        try:
            internal_date_ms = int(payload["internalDate"])
        except (KeyError, TypeError, ValueError) as error:
            raise GmailMessageFetchError(
                "Gmail message internal date was invalid"
            ) from error
        if returned_id != message_id or not thread_id or not history_id:
            raise GmailMessageFetchError("Gmail message response was incomplete")
        label_ids = payload.get("labelIds", [])
        if not isinstance(label_ids, list):
            raise GmailMessageFetchError("Gmail message labels were invalid")
        return GmailRawMessage(
            message_id=returned_id,
            thread_id=thread_id,
            history_id=history_id,
            internal_date_ms=internal_date_ms,
            snippet=str(payload.get("snippet", "")),
            label_ids=tuple(str(item) for item in label_ids),
            payload=_parse_part(payload.get("payload"), depth=0),
        )

    def get_attachment(
        self, message_id: str, attachment_id: str
    ) -> GmailAttachmentBytes:
        """Fetch and base64url-decode one Gmail attachment body."""
        response = self._execute(
            lambda: (
                self._service()
                .users()
                .messages()
                .attachments()
                .get(userId="me", messageId=message_id, id=attachment_id)
            ),
            operation="attachments.get",
        )
        payload = _mapping(response, "Gmail attachment")
        data = payload.get("data")
        if not isinstance(data, str):
            raise GmailMessageFetchError("Gmail attachment data was missing")
        return GmailAttachmentBytes(_decode_base64url(data))

    def ensure_label(self, label_name: str) -> str:
        """Return an exact user Label ID, creating it when absent."""
        normalized = _validate_label_name(label_name)
        response = self._execute(
            lambda: self._service().users().labels().list(userId="me"),
            operation="labels.list",
        )
        payload = _mapping(response, "Gmail Label list")
        labels = payload.get("labels", [])
        if not isinstance(labels, list):
            raise GmailLabelError("Gmail Label list response was invalid")
        for raw in labels:
            item = _mapping(raw, "Gmail Label")
            if item.get("name") == normalized and item.get("id"):
                return str(item["id"])
        response = self._execute(
            lambda: (
                self._service()
                .users()
                .labels()
                .create(
                    userId="me",
                    body={
                        "name": normalized,
                        "labelListVisibility": "labelShow",
                        "messageListVisibility": "show",
                    },
                )
            ),
            operation="labels.create",
        )
        created = _mapping(response, "created Gmail Label")
        label_id = str(created.get("id", ""))
        if not label_id or created.get("name") != normalized:
            raise GmailLabelError("Gmail Label create response was incomplete")
        return label_id

    def add_label_to_messages(
        self, message_ids: tuple[str, ...], label_id: str
    ) -> None:
        """Idempotently apply one Label to at most 1000 message IDs."""
        if not 1 <= len(message_ids) <= 1000:
            raise ValueError("Gmail batchModify requires 1 to 1000 message IDs")
        if any(not message_id.strip() for message_id in message_ids):
            raise ValueError("Gmail message IDs cannot be blank")
        normalized_label_id = label_id.strip()
        if not normalized_label_id:
            raise ValueError("Gmail Label ID cannot be blank")
        self._execute(
            lambda: (
                self._service()
                .users()
                .messages()
                .batchModify(
                    userId="me",
                    body={
                        "ids": list(message_ids),
                        "addLabelIds": [normalized_label_id],
                    },
                )
            ),
            operation="messages.batchModify",
        )

    def _service(self) -> _GmailService:
        if self._service_instance is None:
            try:
                self._service_instance = cast(
                    _GmailService, self._service_factory(self._credentials)
                )
            except Exception as error:
                raise GmailConnectionError(
                    "Gmail API client could not be created"
                ) from error
        return self._service_instance

    def _execute(
        self,
        request_factory: Callable[[], _ExecutableRequest],
        *,
        operation: str,
    ) -> Any:
        last_error: ScribeError | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return request_factory().execute()
            except HttpError as error:
                translated = _translate_http_error(error, operation=operation)
            except (httplib2.HttpLib2Error, OSError) as error:
                translated = GmailConnectionError("Gmail API could not be reached")
                translated.__cause__ = error
            except ScribeError:
                raise
            except Exception as error:
                translated = GmailConnectionError("Gmail API client failed")
                translated.__cause__ = error

            last_error = translated
            if not translated.retryable or attempt >= self._max_attempts:
                raise translated
            delay = self._retry_delay(translated, attempt)
            _LOGGER.warning(
                "Gmail request retry operation=%s attempt=%d delay_seconds=%.3f",
                operation,
                attempt,
                delay,
            )
            self._sleeper(delay)
        if last_error is None:  # pragma: no cover - defensive invariant
            raise GmailConnectionError("Gmail request failed unexpectedly")
        raise last_error

    def _retry_delay(self, error: ScribeError, attempt: int) -> float:
        retry_after = getattr(error, "retry_after_seconds", None)
        exponential = min(
            self._backoff_base_seconds * (2 ** (attempt - 1)),
            self._backoff_max_seconds,
        )
        jitter = float(self._random_source())
        calculated = min(
            exponential + jitter,
            self._backoff_max_seconds,
        )
        if isinstance(retry_after, (int, float)) and retry_after >= 0:
            return float(
                min(
                    max(calculated, float(retry_after)),
                    self._backoff_max_seconds,
                )
            )
        return float(calculated)


def _translate_http_error(error: HttpError, *, operation: str) -> ScribeError:
    status = int(getattr(error.resp, "status", 0))
    reasons = _http_error_reasons(error)
    retry_after = _retry_after_seconds(error)
    if status == 429 or reasons & _RATE_LIMIT_REASONS:
        return GmailRateLimitError(
            "Gmail API rate limit was reached",
            retry_after_seconds=retry_after,
        )
    if status == 404 and operation == "history.list":
        return GmailHistoryExpiredError(
            "Gmail history cursor expired; reconciliation is required"
        )
    if status == 404 and operation.startswith("messages.get"):
        return GmailMessageNotFoundError(
            "Gmail message no longer exists",
            retryable=False,
        )
    if status in {401, 403}:
        return GmailAuthenticationError(
            "Gmail authorization was rejected; run `uv run python -m app auth`"
        )
    if status in _TRANSIENT_HTTP_STATUS:
        if operation.startswith(("labels.", "messages.batchModify")):
            return GmailLabelError(
                f"Gmail {operation} request failed temporarily", retryable=True
            )
        if operation == "profile":
            return GmailConnectionError("Gmail profile request failed temporarily")
        return GmailMessageFetchError(
            f"Gmail {operation} request failed temporarily",
            retryable=True,
        )
    if operation == "profile":
        return GmailConnectionError(
            f"Gmail profile request failed with HTTP {status or 'error'}",
            retryable=False,
        )
    if operation.startswith(("labels.", "messages.batchModify")):
        return GmailLabelError(
            f"Gmail {operation} request failed with HTTP {status or 'error'}"
        )
    return GmailMessageFetchError(
        f"Gmail {operation} request failed with HTTP {status or 'error'}"
    )


def _validate_label_name(label_name: str) -> str:
    normalized = label_name.strip()
    if not normalized or len(normalized) > 200:
        raise ValueError("Gmail Label name must contain 1 to 200 characters")
    if any(ord(character) < 32 for character in normalized):
        raise ValueError("Gmail Label name cannot contain control characters")
    return normalized


def _http_error_reasons(error: HttpError) -> set[str]:
    try:
        payload = json.loads(error.content.decode("utf-8"))
        entries = payload.get("error", {}).get("errors", [])
        return {
            str(entry["reason"])
            for entry in entries
            if isinstance(entry, dict) and "reason" in entry
        }
    except (AttributeError, UnicodeError, json.JSONDecodeError):
        return set()


def _retry_after_seconds(error: HttpError) -> float | None:
    raw = getattr(error, "resp", {}).get("retry-after")
    if raw is None:
        return None
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        try:
            retry_at = parsedate_to_datetime(str(raw))
            return max(0.0, retry_at.timestamp() - time.time())
        except (TypeError, ValueError, OverflowError):
            return None


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise GmailMessageFetchError(f"{name} response was invalid")
    return cast(dict[str, Any], value)


def _parse_headers(raw_headers: Any) -> tuple[GmailHeader, ...]:
    if not isinstance(raw_headers, list):
        raise GmailMessageFetchError("Gmail MIME headers were invalid")
    headers: list[GmailHeader] = []
    for raw in raw_headers:
        item = _mapping(raw, "Gmail MIME header")
        name = item.get("name")
        value = item.get("value")
        if isinstance(name, str) and isinstance(value, str):
            headers.append(GmailHeader(name=name, value=value))
    return tuple(headers)


def _parse_history_record(raw_record: Any) -> tuple[GmailHistoryChange, ...]:
    record = _mapping(raw_record, "Gmail history record")
    changes: list[GmailHistoryChange] = []
    definitions = (
        ("messagesAdded", True, False, False),
        ("messagesDeleted", False, True, False),
        ("labelsAdded", False, False, True),
        ("labelsRemoved", False, False, True),
    )
    for field, added, deleted, has_labels in definitions:
        entries = record.get(field, [])
        if not isinstance(entries, list):
            raise GmailMessageFetchError(f"Gmail history {field} was invalid")
        for raw_entry in entries:
            entry = _mapping(raw_entry, f"Gmail history {field} entry")
            message = _mapping(entry.get("message", {}), "Gmail history message")
            message_id = str(message.get("id", ""))
            thread_id = str(message.get("threadId", ""))
            if not message_id or not thread_id:
                raise GmailMessageFetchError(
                    "Gmail history message identity was incomplete"
                )
            raw_labels = entry.get("labelIds", []) if has_labels else []
            if not isinstance(raw_labels, list):
                raise GmailMessageFetchError("Gmail history labels were invalid")
            labels = tuple(str(item) for item in raw_labels)
            changes.append(
                GmailHistoryChange(
                    message_id=message_id,
                    thread_id=thread_id,
                    message_added=added,
                    message_deleted=deleted,
                    labels_added=labels if field == "labelsAdded" else (),
                    labels_removed=labels if field == "labelsRemoved" else (),
                )
            )
    return tuple(changes)


def _parse_part(raw_part: Any, *, depth: int) -> GmailMessagePart:
    if depth > 50:
        raise GmailMessageFetchError("Gmail MIME nesting exceeded the safe limit")
    payload = _mapping(raw_part, "Gmail MIME part")
    body = _mapping(payload.get("body", {}), "Gmail MIME body")
    try:
        size = int(body.get("size", 0))
    except (TypeError, ValueError) as error:
        raise GmailMessageFetchError("Gmail MIME body size was invalid") from error
    if size < 0:
        raise GmailMessageFetchError("Gmail MIME body size was negative")
    raw_data = body.get("data")
    if raw_data is not None and not isinstance(raw_data, str):
        raise GmailMessageFetchError("Gmail MIME body data was invalid")
    raw_children = payload.get("parts", [])
    if not isinstance(raw_children, list):
        raise GmailMessageFetchError("Gmail MIME child parts were invalid")
    if len(raw_children) > 1000:
        raise GmailMessageFetchError("Gmail MIME part count exceeded the safe limit")
    attachment_id = body.get("attachmentId")
    return GmailMessagePart(
        part_id=str(payload.get("partId", "")),
        mime_type=str(payload.get("mimeType", "application/octet-stream")).lower(),
        filename=str(payload.get("filename", "")),
        headers=_parse_headers(payload.get("headers", [])),
        body_size=size,
        body_data=_decode_base64url(raw_data) if raw_data else None,
        attachment_id=str(attachment_id) if attachment_id else None,
        parts=tuple(_parse_part(child, depth=depth + 1) for child in raw_children),
    )


def _decode_base64url(value: str) -> bytes:
    try:
        padded = value + "=" * (-len(value) % 4)
        return base64.b64decode(
            padded.encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
    except (UnicodeEncodeError, binascii.Error, ValueError) as error:
        raise GmailMessageFetchError(
            "Gmail returned invalid base64url content"
        ) from error
