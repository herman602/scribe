"""Mocked Gmail synchronization endpoint and retry tests."""

from __future__ import annotations

import base64
from typing import Any

import httplib2  # type: ignore[import-untyped]
import pytest
from app.domain.errors import GmailHistoryExpiredError, GmailMessageFetchError
from app.infrastructure.gmail.gateway import GoogleGmailGateway
from google.oauth2.credentials import Credentials
from googleapiclient.errors import HttpError  # type: ignore[import-untyped]


class _Request:
    def __init__(self, value: Any) -> None:
        self.value = value

    def execute(self) -> Any:
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class _Attachments:
    def __init__(self, responses: dict[str, Any], calls: list[tuple[str, Any]]) -> None:
        self.responses = responses
        self.calls = calls

    def get(self, **kwargs: Any) -> _Request:
        self.calls.append(("attachments.get", kwargs))
        return _Request(self.responses["attachment"])


class _Messages:
    def __init__(self, responses: dict[str, Any], calls: list[tuple[str, Any]]) -> None:
        self.responses = responses
        self.calls = calls

    def list(self, **kwargs: Any) -> _Request:
        self.calls.append(("messages.list", kwargs))
        value = self.responses["list"]
        if isinstance(value, list):
            value = value.pop(0)
        return _Request(value)

    def get(self, **kwargs: Any) -> _Request:
        self.calls.append(("messages.get", kwargs))
        return _Request(self.responses["message"])

    def batchModify(self, **kwargs: Any) -> _Request:
        self.calls.append(("messages.batchModify", kwargs))
        return _Request(self.responses.get("batchModify", {}))

    def attachments(self) -> _Attachments:
        return _Attachments(self.responses, self.calls)


class _Threads:
    def __init__(self, responses: dict[str, Any], calls: list[tuple[str, Any]]) -> None:
        self.responses = responses
        self.calls = calls

    def get(self, **kwargs: Any) -> _Request:
        self.calls.append(("threads.get", kwargs))
        return _Request(self.responses["thread"])


class _History:
    def __init__(self, responses: dict[str, Any], calls: list[tuple[str, Any]]) -> None:
        self.responses = responses
        self.calls = calls

    def list(self, **kwargs: Any) -> _Request:
        self.calls.append(("history.list", kwargs))
        return _Request(self.responses["history"])


class _Labels:
    def __init__(self, responses: dict[str, Any], calls: list[tuple[str, Any]]) -> None:
        self.responses = responses
        self.calls = calls

    def list(self, **kwargs: Any) -> _Request:
        self.calls.append(("labels.list", kwargs))
        return _Request(self.responses.get("labels", {"labels": []}))

    def create(self, **kwargs: Any) -> _Request:
        self.calls.append(("labels.create", kwargs))
        return _Request(
            self.responses.get(
                "label_create", {"id": "Label_42", "name": "Scribe/已處理"}
            )
        )


class _Users:
    def __init__(self, responses: dict[str, Any], calls: list[tuple[str, Any]]) -> None:
        self.responses = responses
        self.calls = calls

    def getProfile(self, *, userId: str) -> _Request:
        self.calls.append(("profile", {"userId": userId}))
        return _Request(self.responses["profile"])

    def messages(self) -> _Messages:
        return _Messages(self.responses, self.calls)

    def threads(self) -> _Threads:
        return _Threads(self.responses, self.calls)

    def history(self) -> _History:
        return _History(self.responses, self.calls)

    def labels(self) -> _Labels:
        return _Labels(self.responses, self.calls)


class _Service:
    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, Any]] = []

    def users(self) -> _Users:
        return _Users(self.responses, self.calls)


def _credentials() -> Credentials:
    return Credentials(token="fixture")  # type: ignore[no-untyped-call]


def _b64(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode().rstrip("=")


def _responses() -> dict[str, Any]:
    return {
        "profile": {
            "emailAddress": "user@example.com",
            "messagesTotal": 2,
            "threadsTotal": 1,
            "historyId": "100",
        },
        "list": {
            "messages": [{"id": "m1", "threadId": "t1"}],
            "nextPageToken": "next",
            "resultSizeEstimate": 2,
        },
        "thread": {
            "id": "t1",
            "messages": [
                {
                    "id": "m1",
                    "threadId": "t1",
                    "payload": {
                        "headers": [{"name": "From", "value": "notice@bank.example"}]
                    },
                },
                {
                    "id": "m2",
                    "threadId": "t1",
                    "payload": {
                        "headers": [{"name": "From", "value": "notice@bank.example"}]
                    },
                },
            ],
        },
        "message": {
            "id": "m1",
            "threadId": "t1",
            "historyId": "101",
            "internalDate": "1721000000000",
            "snippet": "fixture",
            "labelIds": ["INBOX"],
            "payload": {
                "partId": "",
                "mimeType": "text/plain",
                "filename": "",
                "headers": [
                    {"name": "From", "value": "notice@bank.example"},
                    {
                        "name": "Content-Type",
                        "value": "text/plain; charset=utf-8",
                    },
                ],
                "body": {"size": 7, "data": _b64(b"fixture")},
            },
        },
        "attachment": {"size": 8, "data": _b64(b"%PDF-1.7")},
        "history": {
            "historyId": "105",
            "nextPageToken": "history-next",
            "history": [
                {
                    "id": "102",
                    "messagesAdded": [{"message": {"id": "m1", "threadId": "t1"}}],
                    "labelsAdded": [
                        {
                            "message": {"id": "m1", "threadId": "t1"},
                            "labelIds": ["INBOX"],
                        }
                    ],
                    "messagesDeleted": [{"message": {"id": "m2", "threadId": "t1"}}],
                }
            ],
        },
    }


def test_gateway_calls_message_thread_and_attachment_endpoints() -> None:
    service = _Service(_responses())
    gateway = GoogleGmailGateway(
        _credentials(),
        service_factory=lambda credentials: service,
    )

    page = gateway.list_messages(
        query="newer_than:2y",
        page_token="page-1",
        max_results=50,
        include_spam_trash=False,
    )
    thread = gateway.get_thread("t1")
    message = gateway.get_message("m1")
    attachment = gateway.get_attachment("m1", "a1")

    assert page.messages[0].message_id == "m1"
    assert page.next_page_token == "next"
    assert [item.message_id for item in thread.messages] == ["m1", "m2"]
    assert message.payload.body_data == b"fixture"
    assert attachment.data == b"%PDF-1.7"
    calls = dict(service.calls)
    assert calls["messages.list"]["q"] == "newer_than:2y"
    assert calls["messages.list"]["pageToken"] == "page-1"
    assert calls["threads.get"]["format"] == "metadata"
    assert calls["threads.get"]["metadataHeaders"] == ["From"]
    assert calls["messages.get"]["format"] == "full"


def test_gateway_creates_and_applies_message_level_label() -> None:
    service = _Service(_responses())
    gateway = GoogleGmailGateway(
        _credentials(), service_factory=lambda credentials: service
    )

    label_id = gateway.ensure_label("Scribe/已處理")
    gateway.add_label_to_messages(("m1", "m2"), label_id)

    assert label_id == "Label_42"
    calls = dict(service.calls)
    assert calls["labels.create"]["body"]["name"] == "Scribe/已處理"
    assert calls["messages.batchModify"]["body"] == {
        "ids": ["m1", "m2"],
        "addLabelIds": ["Label_42"],
    }


def test_gateway_reuses_existing_exact_label() -> None:
    responses = _responses()
    responses["labels"] = {
        "labels": [{"id": "Label_existing", "name": "Scribe/已處理"}]
    }
    service = _Service(responses)
    gateway = GoogleGmailGateway(
        _credentials(), service_factory=lambda credentials: service
    )

    assert gateway.ensure_label(" Scribe/已處理 ") == "Label_existing"
    assert all(name != "labels.create" for name, _ in service.calls)


def test_gateway_retries_rate_limit_and_honors_retry_after() -> None:
    responses = _responses()
    response = httplib2.Response({"status": "429", "retry-after": "3"})
    responses["list"] = [
        HttpError(response, b"{}", uri="https://gmail.googleapis.test"),
        {"messages": [], "resultSizeEstimate": 0},
    ]
    service = _Service(responses)
    sleeps: list[float] = []
    gateway = GoogleGmailGateway(
        _credentials(),
        max_attempts=2,
        service_factory=lambda credentials: service,
        sleeper=sleeps.append,
        random_source=lambda: 0.5,
    )

    page = gateway.list_messages(
        query=None,
        page_token=None,
        max_results=10,
        include_spam_trash=False,
    )

    assert page.messages == ()
    assert sleeps == [3.0]
    assert [name for name, _ in service.calls].count("messages.list") == 2


def test_gateway_stops_after_bounded_transient_retries() -> None:
    responses = _responses()
    response = httplib2.Response({"status": "503"})
    responses["message"] = HttpError(
        response, b"{}", uri="https://gmail.googleapis.test"
    )
    service = _Service(responses)
    sleeps: list[float] = []
    gateway = GoogleGmailGateway(
        _credentials(),
        max_attempts=3,
        service_factory=lambda credentials: service,
        sleeper=sleeps.append,
        random_source=lambda: 0.0,
    )

    with pytest.raises(GmailMessageFetchError) as caught:
        gateway.get_message("m1")

    assert caught.value.retryable
    assert sleeps == [1.0, 2.0]
    assert [name for name, _ in service.calls].count("messages.get") == 3


def test_gateway_rejects_invalid_page_size_and_base64() -> None:
    responses = _responses()
    responses["attachment"] = {"data": "not base64!"}
    gateway = GoogleGmailGateway(
        _credentials(),
        service_factory=lambda credentials: _Service(responses),
    )

    with pytest.raises(ValueError, match="between 1 and 500"):
        gateway.list_messages(
            query=None,
            page_token=None,
            max_results=501,
            include_spam_trash=False,
        )
    with pytest.raises(GmailMessageFetchError, match="base64url"):
        gateway.get_attachment("m1", "a1")


def test_gateway_parses_history_and_metadata_message_by_message() -> None:
    service = _Service(_responses())
    gateway = GoogleGmailGateway(
        _credentials(),
        service_factory=lambda credentials: service,
    )

    page = gateway.list_history(
        start_history_id="100",
        page_token="cursor",
        max_results=50,
    )
    metadata = gateway.get_message_metadata("m1")

    assert page.latest_history_id == "105"
    assert page.next_page_token == "history-next"
    assert len(page.changes) == 3
    assert page.changes[0].message_added
    assert page.changes[1].message_deleted
    assert page.changes[2].labels_added == ("INBOX",)
    assert metadata.message_id == "m1"
    assert metadata.sender_header == "notice@bank.example"
    calls = dict(service.calls)
    assert calls["history.list"]["startHistoryId"] == "100"
    assert calls["history.list"]["pageToken"] == "cursor"
    assert calls["messages.get"]["format"] == "metadata"


def test_gateway_translates_history_404_to_expired_cursor() -> None:
    responses = _responses()
    responses["history"] = HttpError(
        httplib2.Response({"status": "404"}),
        b"{}",
        uri="https://gmail.googleapis.test",
    )
    gateway = GoogleGmailGateway(
        _credentials(),
        service_factory=lambda credentials: _Service(responses),
    )

    with pytest.raises(GmailHistoryExpiredError):
        gateway.list_history(
            start_history_id="expired",
            page_token=None,
            max_results=50,
        )
