"""Tests for HttpClient retry rules, including non-idempotent requests."""

from __future__ import annotations

import http.client

import pytest

from ledgerdemain.http_client import HttpClient, HttpError, HttpResponse, OutcomeUnknownError


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
    assert not isinstance(exc_info.value, OutcomeUnknownError)
    assert transport.calls == 3


@pytest.mark.parametrize(
    "outcome",
    [
        TimeoutError("read timed out"),
        ConnectionResetError("reset"),
        http.client.IncompleteRead(b"{"),
        _status(500),
        _status(502),
        _status(503),
        _status(504),
    ],
)
def test_non_idempotent_request_is_not_retried_when_outcome_unknown(
    outcome: HttpResponse | Exception,
) -> None:
    transport = _ScriptedTransport(outcome, _ok())
    with pytest.raises(OutcomeUnknownError):
        _client(transport).request_bytes("POST", "https://example.test", idempotent=False)
    assert transport.calls == 1


def test_non_idempotent_request_still_retries_rate_limit() -> None:
    transport = _ScriptedTransport(_status(429), _ok())
    resp = _client(transport).request_bytes("POST", "https://example.test", idempotent=False)
    assert resp.status == 200
    assert transport.calls == 2


def test_non_idempotent_client_error_is_a_plain_http_error() -> None:
    transport = _ScriptedTransport(_status(400))
    with pytest.raises(HttpError) as exc_info:
        _client(transport).request_bytes("POST", "https://example.test", idempotent=False)
    assert exc_info.value.status == 400
    assert not isinstance(exc_info.value, OutcomeUnknownError)


def test_idempotent_request_still_retries_server_errors() -> None:
    transport = _ScriptedTransport(_status(503), _ok())
    resp = _client(transport).request_bytes("POST", "https://example.test")
    assert resp.status == 200
    assert transport.calls == 2
