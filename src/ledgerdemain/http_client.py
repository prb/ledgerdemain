"""HTTP client with injectable transport, rate-limit backoff, and GraphQL support."""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, ClassVar, Protocol


@dataclass(slots=True)
class HttpResponse:
    """Raw HTTP response."""

    status: int
    headers: dict[str, str]
    body: bytes


class HttpError(RuntimeError):
    """Raised when an HTTP request fails after retries."""

    def __init__(self, status: int, message: str, body: str = "") -> None:
        super().__init__(f"HTTP {status}: {message}")
        self.status = status
        self.message = message
        self.body = body


class RateLimitError(HttpError):
    """Raised when HTTP 429 persists past all retry attempts."""


class GraphQLError(RuntimeError):
    """Raised when a GraphQL endpoint returns top-level errors."""

    def __init__(self, errors: list[dict[str, Any]]) -> None:
        summary = "; ".join(str(err.get("message", err)) for err in errors)
        super().__init__(f"GraphQL error: {summary}")
        self.errors = errors


class HttpTransport(Protocol):
    """Pluggable HTTP transport interface for production and test mocking."""

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse: ...


class UrllibTransport:
    """Standard-library HTTP transport powered by urllib.request."""

    def request(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        req = urllib.request.Request(
            url=url,
            data=body,
            headers=headers or {},
            method=method.upper(),
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                resp_headers = {k: v for k, v in resp.headers.items()}
                return HttpResponse(status=int(resp.status), headers=resp_headers, body=resp.read())
        except urllib.error.HTTPError as err:
            resp_headers = {k: v for k, v in err.headers.items()} if err.headers else {}
            err_body = err.read() if err.fp is not None else b""
            return HttpResponse(status=int(err.code), headers=resp_headers, body=err_body)


def _get_header_ci(headers: dict[str, str], name: str) -> str | None:
    target = name.lower()
    for key, val in headers.items():
        if key.lower() == target:
            return val
    return None


class HttpClient:
    """Resilient HTTP/JSON/GraphQL client with exponential backoff and Retry-After handling."""

    RETRYABLE_STATUSES: ClassVar[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

    def __init__(
        self,
        transport: HttpTransport | None = None,
        max_retries: int = 3,
        base_delay: float = 0.5,
        sleep_fn: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ) -> None:
        self.transport: HttpTransport = transport or UrllibTransport()
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.sleep_fn = sleep_fn
        self.timeout = timeout

    def request_bytes(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> HttpResponse:
        """Perform an HTTP request with retry handling for transient and 429 responses."""
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.transport.request(
                    method=method,
                    url=url,
                    headers=headers,
                    body=body,
                    timeout=self.timeout,
                )
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                last_exc = exc
                if attempt < self.max_retries:
                    self.sleep_fn(self.base_delay * (2**attempt))
                    continue
                raise HttpError(0, f"Network request failed: {exc}") from exc

            if resp.status in self.RETRYABLE_STATUSES and attempt < self.max_retries:
                delay = self.base_delay * (2**attempt)
                if resp.status == 429:
                    retry_after = _get_header_ci(resp.headers, "Retry-After")
                    if retry_after is not None:
                        with contextlib.suppress(ValueError):
                            delay = max(float(retry_after), 0.0)
                self.sleep_fn(delay)
                continue

            if resp.status == 429:
                body_text = resp.body.decode("utf-8", errors="replace")
                raise RateLimitError(429, "Rate limit exceeded", body=body_text)

            if resp.status >= 400:
                body_text = resp.body.decode("utf-8", errors="replace")
                raise HttpError(resp.status, body_text[:300] or "Request failed", body=body_text)

            return resp

        raise HttpError(0, f"Request failed after retries: {last_exc}")

    def request_json(
        self,
        method: str,
        url: str,
        headers: dict[str, str] | None = None,
        json_body: Any = None,
        params: dict[str, Any] | None = None,
    ) -> Any:
        """Send an HTTP request and decode a JSON response."""
        full_url = url
        if params:
            filtered = {k: str(v) for k, v in params.items() if v is not None}
            if filtered:
                sep = "&" if "?" in url else "?"
                full_url = f"{url}{sep}{urllib.parse.urlencode(filtered)}"

        req_headers = dict(headers or {})
        body_bytes: bytes | None = None
        if json_body is not None:
            body_bytes = json.dumps(json_body).encode("utf-8")
            if _get_header_ci(req_headers, "Content-Type") is None:
                req_headers["Content-Type"] = "application/json"

        resp = self.request_bytes(method=method, url=full_url, headers=req_headers, body=body_bytes)
        if not resp.body:
            return {}
        return json.loads(resp.body.decode("utf-8"))

    def post_graphql(
        self,
        endpoint: str,
        access_token: str,
        query: str,
        variables: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute a GraphQL POST operation and raise GraphQLError on top-level errors."""
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        payload = {"query": query, "variables": variables or {}}
        result = self.request_json("POST", endpoint, headers=headers, json_body=payload)
        if not isinstance(result, dict):
            raise HttpError(500, f"Unexpected GraphQL response shape: {type(result).__name__}")
        errors = result.get("errors")
        if errors:
            raise GraphQLError(errors)
        data = result.get("data")
        return data if isinstance(data, dict) else {}
