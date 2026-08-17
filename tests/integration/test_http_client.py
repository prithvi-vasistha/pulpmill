"""The shared HTTP client: retries, backoff, 429 handling, redaction."""

from __future__ import annotations

import random

import httpx
import pytest

from pulpmill.config.models import HttpConfig, RateLimitConfig, RetryConfig
from pulpmill.domain.errors import SourceRequestError
from pulpmill.infrastructure.http import HttpClient, safe_headers
from tests.support.clock import ManualClock


def build_client(
    handler,
    clock: ManualClock,
    *,
    retry: RetryConfig | None = None,
    rate_limit: RateLimitConfig | None = None,
) -> HttpClient:
    return HttpClient(
        name="test",
        config=HttpConfig(retry=retry or RetryConfig(jitter_ratio=0.0)),
        rate_limit=rate_limit or RateLimitConfig(requests_per_second=1000.0, burst=100),
        clock=clock,
        rng=random.Random(0),
        transport=httpx.MockTransport(handler),
    )


class TestSuccess:
    def test_a_successful_request_returns_the_response(self, clock: ManualClock) -> None:
        client = build_client(lambda r: httpx.Response(200, json={"ok": True}), clock)
        assert client.get_json("https://example.com/x") == {"ok": True}
        assert clock.total_slept == 0.0
        client.close()

    def test_a_non_json_body_raises_a_typed_error(self, clock: ManualClock) -> None:
        client = build_client(lambda r: httpx.Response(200, text="<html>nope</html>"), clock)
        with pytest.raises(SourceRequestError, match="not valid JSON"):
            client.get_json("https://example.com/x")
        client.close()

    def test_the_user_agent_is_sent(self, clock: ManualClock) -> None:
        seen: list[str] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers["User-Agent"])
            return httpx.Response(200, json={})

        client = HttpClient(
            name="test",
            config=HttpConfig(),
            rate_limit=RateLimitConfig(requests_per_second=100.0, burst=10),
            user_agent="linux:pulpmill:test (by /u/tester)",
            clock=clock,
            transport=httpx.MockTransport(handle),
        )
        client.get_json("https://example.com/x")
        assert seen == ["linux:pulpmill:test (by /u/tester)"]
        client.close()


class TestRetries:
    def test_a_transient_failure_is_retried_then_succeeds(self, clock: ManualClock) -> None:
        attempts = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                return httpx.Response(503)
            return httpx.Response(200, json={"ok": True})

        client = build_client(handle, clock)
        assert client.get_json("https://example.com/x") == {"ok": True}
        assert attempts["n"] == 3
        assert clock.sleeps == [1.0, 2.0]  # exponential, jitter disabled
        client.close()

    def test_the_attempt_budget_is_respected(self, clock: ManualClock) -> None:
        attempts = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(500)

        client = build_client(handle, clock, retry=RetryConfig(max_attempts=3, jitter_ratio=0.0))
        with pytest.raises(SourceRequestError) as exc:
            client.get_json("https://example.com/x")
        assert attempts["n"] == 3
        assert exc.value.attempts == 3
        assert exc.value.status_code == 500
        client.close()

    @pytest.mark.parametrize("status", [400, 401, 403, 404])
    def test_client_errors_fail_immediately(self, clock: ManualClock, status: int) -> None:
        """Never hammer a source over an auth or not-found error."""
        attempts = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            return httpx.Response(status)

        client = build_client(handle, clock)
        with pytest.raises(SourceRequestError):
            client.get_json("https://example.com/x")
        assert attempts["n"] == 1
        assert clock.total_slept == 0.0
        client.close()

    def test_transport_errors_are_retried(self, clock: ManualClock) -> None:
        attempts = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 2:
                raise httpx.ConnectTimeout("connection timed out")
            return httpx.Response(200, json={"ok": True})

        client = build_client(handle, clock)
        assert client.get_json("https://example.com/x") == {"ok": True}
        assert attempts["n"] == 2
        client.close()

    def test_persistent_transport_errors_surface_as_a_typed_failure(
        self, clock: ManualClock
    ) -> None:
        def handle(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        client = build_client(handle, clock, retry=RetryConfig(max_attempts=2, jitter_ratio=0.0))
        with pytest.raises(SourceRequestError, match="transport failure"):
            client.get_json("https://example.com/x")
        client.close()


class TestRateLimitHandling:
    def test_a_429_is_retried_and_honours_retry_after(self, clock: ManualClock) -> None:
        attempts = {"n": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return httpx.Response(429, headers={"retry-after": "12"})
            return httpx.Response(200, json={"ok": True})

        client = build_client(handle, clock)
        assert client.get_json("https://example.com/x") == {"ok": True}
        assert clock.total_slept >= 12.0
        client.close()

    def test_a_429_stalls_the_whole_source_not_just_the_request(self, clock: ManualClock) -> None:
        """Otherwise the very next request sails straight past the block."""

        def handle(request: httpx.Request) -> httpx.Response:
            return httpx.Response(429, headers={"retry-after": "30"})

        client = build_client(handle, clock, retry=RetryConfig(max_attempts=1, jitter_ratio=0.0))
        with pytest.raises(SourceRequestError):
            client.get_json("https://example.com/x")
        assert client.rate_limiter.time_until_available() > 0
        client.close()

    def test_an_excessive_retry_after_fails_fast(self, clock: ManualClock) -> None:
        client = build_client(
            lambda r: httpx.Response(429, headers={"retry-after": "7200"}),
            clock,
            retry=RetryConfig(max_retry_after_seconds=60.0, jitter_ratio=0.0),
        )
        with pytest.raises(SourceRequestError):
            client.get_json("https://example.com/x")
        # We refused to block for two hours.
        assert clock.total_slept < 60.0
        client.close()

    def test_the_configured_request_rate_is_enforced(self, clock: ManualClock) -> None:
        client = build_client(
            lambda r: httpx.Response(200, json={}),
            clock,
            rate_limit=RateLimitConfig(requests_per_second=1.0, burst=1),
        )
        start = clock.monotonic()
        for _ in range(4):
            client.get_json("https://example.com/x")
        assert clock.monotonic() - start == pytest.approx(3.0, abs=0.01)
        client.close()


class TestSecretRedaction:
    @pytest.mark.parametrize(
        "header", ["Authorization", "Cookie", "Set-Cookie", "Proxy-Authorization"]
    )
    def test_credential_headers_are_masked(self, header: str) -> None:
        masked = safe_headers({header: "super-secret-value", "Accept": "application/json"})
        assert masked[header] == "***redacted***"
        assert masked["Accept"] == "application/json"

    def test_masking_is_case_insensitive(self) -> None:
        assert safe_headers({"authorization": "bearer x"})["authorization"] == "***redacted***"

    def test_the_log_redaction_processor_masks_secret_keys(self) -> None:
        from pulpmill.infrastructure.logging import _redact_secrets

        event = {
            "event": "request",
            "api_key": "sk-live-1234",
            "client_secret": "shhh",
            "access_token": "tok",
            "password": "hunter2",
            "url": "https://example.com",
            "headers": {"Authorization": "bearer abc", "Accept": "json"},
        }
        redacted = _redact_secrets(None, "info", event)
        for key in ("api_key", "client_secret", "access_token", "password"):
            assert redacted[key] == "***redacted***"
        assert redacted["url"] == "https://example.com"
        assert redacted["headers"]["Authorization"] == "***redacted***"
        assert redacted["headers"]["Accept"] == "json"
