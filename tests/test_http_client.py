"""Tests for HttpClient retry rules."""

from __future__ import annotations

import http.client

import pytest

from ledgerdemain.http_client import HttpClient, HttpError, HttpResponse


class _ScriptedTransport:
    """Transport that replays a fixed sequence of responses or exceptions."""

    def __init__(self, *outcomes: HttpResponse | Exception) -> None:
        self.outcomes = list(outcomes)
        self.calls = 0

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        self.calls += 1
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ok() -> HttpResponse:
    return HttpResponse(status=200, headers={}, body=b'{"ok": true}')


def _status(code: int) -> HttpResponse:
    return HttpResponse(status=code, headers={}, body=b"upstream said no")


def _client(transport: _ScriptedTransport) -> HttpClient:
    return HttpClient(transport=transport, max_retries=2, sleep_fn=lambda _: None)


def test_truncated_response_is_retried_for_idempotent_requests() -> None:
    transport = _ScriptedTransport(http.client.IncompleteRead(b"{"), _ok())
    assert _client(transport).request_json("GET", "https://example.test") == {"ok": True}
    assert transport.calls == 2


@pytest.mark.parametrize(
    "exc",
    [http.client.IncompleteRead(b"{"), http.client.BadStatusLine("garbage")],
)
def test_persistent_http_exception_becomes_http_error(exc: Exception) -> None:
    transport = _ScriptedTransport(exc, exc, exc)
    with pytest.raises(HttpError) as exc_info:
        _client(transport).request_bytes("GET", "https://example.test")
    assert exc_info.value.status == 0
    assert transport.calls == 3
