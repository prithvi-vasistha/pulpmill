"""The HTTP client every adapter uses.

Responsibilities, all in one place so no adapter has to remember them:

* bounded connection pool and explicit timeouts on all four phases
* client-side token-bucket rate limiting per source
* retries with exponential backoff and jitter
* `429` handling that also stalls the bucket, not just the one request
* transient `5xx` handling; non-retryable statuses fail immediately
* structured logging of every attempt, with credentials redacted

It deliberately does not attempt to work around blocks. A `403` is a `403`:
raised, logged, and left for a human to resolve.
"""

from __future__ import annotations

import random
from collections.abc import Mapping
from types import TracebackType
from typing import Any

import httpx

from pulpmill.config.models import HttpConfig, RateLimitConfig
from pulpmill.domain.errors import SourceRequestError
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock
from pulpmill.infrastructure.logging import get_logger
from pulpmill.infrastructure.ratelimit import TokenBucket
from pulpmill.infrastructure.retry import RetryPolicy, parse_retry_after

#: Transport failures worth retrying. Anything else propagates untouched.
_TRANSIENT_EXCEPTIONS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
    httpx.ReadError,
    httpx.WriteError,
)

#: Header names never written to a log, regardless of value.
_SENSITIVE_HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})


def safe_headers(headers: Mapping[str, str]) -> dict[str, str]:
    """Copy of `headers` with credential-bearing values masked."""
    return {
        key: ("***redacted***" if key.lower() in _SENSITIVE_HEADERS else value)
        for key, value in headers.items()
    }


class HttpClient:
    """A rate-limited, retrying HTTP client scoped to one source."""

    def __init__(
        self,
        *,
        name: str,
        config: HttpConfig,
        rate_limit: RateLimitConfig,
        user_agent: str | None = None,
        default_headers: Mapping[str, str] | None = None,
        clock: Clock = SYSTEM_CLOCK,
        rng: random.Random | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self._name = name
        self._config = config
        self._clock = clock
        self._policy = RetryPolicy(config.retry, rng=rng)
        self._bucket = TokenBucket(
            rate_per_second=rate_limit.requests_per_second,
            capacity=rate_limit.burst,
            clock=clock,
        )
        self._log = get_logger("http", source=name)

        headers = {
            "User-Agent": user_agent or config.user_agent,
            "Accept-Encoding": "gzip, deflate",
        }
        if default_headers:
            headers.update(default_headers)

        self._client = httpx.Client(
            headers=headers,
            timeout=httpx.Timeout(
                connect=config.timeout.connect_seconds,
                read=config.timeout.read_seconds,
                write=config.timeout.write_seconds,
                pool=config.timeout.pool_seconds,
            ),
            limits=httpx.Limits(
                max_connections=config.pool.max_connections,
                max_keepalive_connections=config.pool.max_keepalive_connections,
                keepalive_expiry=config.pool.keepalive_expiry_seconds,
            ),
            follow_redirects=True,
            transport=transport,
        )

    @property
    def rate_limiter(self) -> TokenBucket:
        """Exposed so an adapter can apply back-pressure it learned from headers."""
        return self._bucket

    def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        data: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        content: bytes | None = None,
        auth: tuple[str, str] | None = None,
        expected_status: tuple[int, ...] = (200,),
    ) -> httpx.Response:
        """Perform a request, retrying per policy.

        `content` sends a raw body, which is what resumable media uploads need.
        It is deliberately `bytes` rather than a file handle: a retry has to be
        able to send the same body again, and a consumed stream cannot.

        Raises `SourceRequestError` when the attempt budget is exhausted or the
        status is not retryable and not expected.
        """
        last_error: str = "no attempt was made"
        last_status: int | None = None

        for attempt in range(1, self._policy.max_attempts + 1):
            waited = self._bucket.acquire()
            if waited > 0.05:
                self._log.debug("rate_limit_wait", url=url, waited_seconds=round(waited, 3))

            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    headers=dict(headers) if headers else None,
                    data=data,
                    json=json_body,
                    content=content,
                    auth=auth,
                )
            except _TRANSIENT_EXCEPTIONS as exc:
                decision = self._policy.on_exception(exc, attempt=attempt)
                last_error = f"{type(exc).__name__}: {exc}"
                self._log.warning(
                    "http_transport_error",
                    url=url,
                    method=method,
                    attempt=attempt,
                    error_type=type(exc).__name__,
                    error=str(exc),
                    will_retry=decision.should_retry,
                    retry_in_seconds=round(decision.delay_seconds, 2),
                )
                if not decision.should_retry:
                    raise SourceRequestError(
                        "transport failure",
                        url=url,
                        attempts=attempt,
                        source=self._name,
                        detail=last_error,
                    ) from exc
                self._clock.sleep(decision.delay_seconds)
                continue

            last_status = response.status_code

            if response.status_code in expected_status:
                self._log.debug(
                    "http_request",
                    url=url,
                    method=method,
                    status=response.status_code,
                    attempt=attempt,
                    bytes=len(response.content),
                )
                return response

            decision = self._policy.on_status(
                response.status_code, attempt=attempt, headers=response.headers
            )
            last_error = f"unexpected status {response.status_code}"

            if response.status_code == 429:
                # Apply the penalty to the whole source: the next request on this
                # bucket must not sail straight past the block.
                retry_after_raw = response.headers.get("retry-after")
                penalty = (
                    parse_retry_after(retry_after_raw)
                    if retry_after_raw
                    else decision.delay_seconds
                ) or decision.delay_seconds
                self._bucket.pause_for(penalty)
                self._log.warning(
                    "http_rate_limited",
                    url=url,
                    attempt=attempt,
                    retry_after_seconds=round(penalty, 2),
                    will_retry=decision.should_retry,
                )
            else:
                self._log.warning(
                    "http_unexpected_status",
                    url=url,
                    method=method,
                    status=response.status_code,
                    attempt=attempt,
                    will_retry=decision.should_retry,
                    reason=decision.reason,
                    body_preview=response.text[:200] if response.content else "",
                )

            response.close()

            if not decision.should_retry:
                raise SourceRequestError(
                    "request failed",
                    url=url,
                    status_code=response.status_code,
                    attempts=attempt,
                    source=self._name,
                    detail=decision.reason,
                )

            self._clock.sleep(decision.delay_seconds)

        raise SourceRequestError(
            "retries exhausted",
            url=url,
            status_code=last_status,
            attempts=self._policy.max_attempts,
            source=self._name,
            detail=last_error,
        )

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        expected_status: tuple[int, ...] = (200,),
    ) -> Any:
        """GET and parse JSON, turning a malformed body into a typed error."""
        response = self.request(
            "GET", url, params=params, headers=headers, expected_status=expected_status
        )
        try:
            return response.json()
        except ValueError as exc:
            raise SourceRequestError(
                "response body was not valid JSON",
                url=url,
                status_code=response.status_code,
                source=self._name,
                detail=response.text[:200],
            ) from exc

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpClient:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
