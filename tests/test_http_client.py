"""Tests for HttpClient retry rules, including non-idempotent requests."""

from __future__ import annotations

import http.client

import pytest

from ledgerdemain.http_client import (
    GraphQLError,
    HttpClient,
    HttpError,
    HttpResponse,
    OutcomeUnknownError,
)


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


def _ok_with(body: bytes) -> _ScriptedTransport:
    return _ScriptedTransport(HttpResponse(status=200, headers={}, body=body))


@pytest.mark.parametrize("body", [b"", b"not json", b"\xff\xfe"])
def test_non_idempotent_unusable_body_is_outcome_unknown(body: bytes) -> None:
    transport = _ok_with(body)
    with pytest.raises(OutcomeUnknownError, match="may have been applied"):
        _client(transport).request_json("POST", "https://example.test", idempotent=False)
    assert transport.calls == 1


def test_idempotent_empty_body_still_returns_empty_dict() -> None:
    assert _client(_ok_with(b"")).request_json("GET", "https://example.test") == {}


def test_idempotent_invalid_json_still_raises_value_error() -> None:
    with pytest.raises(ValueError) as exc_info:
        _client(_ok_with(b"not json")).request_json("GET", "https://example.test")
    assert not isinstance(exc_info.value, OutcomeUnknownError)


def _post(body: bytes, *, idempotent: bool) -> dict[str, object]:
    return _client(_ok_with(body)).post_graphql(
        "https://example.test", "token", "query { x }", idempotent=idempotent
    )


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"{}", b'{"data": null}'])
def test_non_idempotent_graphql_without_data_or_errors_is_outcome_unknown(body: bytes) -> None:
    with pytest.raises(OutcomeUnknownError, match="may have been applied"):
        _post(body, idempotent=False)


def test_non_idempotent_graphql_errors_stay_definite() -> None:
    with pytest.raises(GraphQLError):
        _post(b'{"errors": [{"message": "bad"}], "data": null}', idempotent=False)


def test_idempotent_graphql_empty_body_still_returns_empty_data() -> None:
    assert _post(b"", idempotent=True) == {}
    assert _post(b"{}", idempotent=True) == {}
    assert _post(b'{"data": null}', idempotent=True) == {}


def test_idempotent_graphql_non_object_is_still_http_error() -> None:
    with pytest.raises(HttpError) as exc_info:
        _post(b"[]", idempotent=True)
    assert not isinstance(exc_info.value, OutcomeUnknownError)
