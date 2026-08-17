"""Retry policy and rate limiting.

These are what keep a 24/7 worker from being blocked, so their behaviour is
pinned precisely rather than approximately.
"""

from __future__ import annotations

import random
from email.utils import format_datetime

import pytest

from pulpmill.config.models import RetryConfig
from pulpmill.infrastructure.clock import utc_now
from pulpmill.infrastructure.ratelimit import TokenBucket
from pulpmill.infrastructure.retry import RetryPolicy, parse_retry_after
from tests.support.clock import ManualClock


def policy(**overrides: object) -> RetryPolicy:
    config = RetryConfig(jitter_ratio=0.0, **overrides)  # type: ignore[arg-type]
    return RetryPolicy(config, rng=random.Random(0))


class TestBackoff:
    def test_delays_grow_exponentially(self) -> None:
        retry = policy(initial_backoff_seconds=1.0, multiplier=2.0)
        assert [retry.backoff_for(n) for n in (1, 2, 3, 4)] == [1.0, 2.0, 4.0, 8.0]

    def test_delays_are_capped(self) -> None:
        retry = policy(initial_backoff_seconds=1.0, multiplier=10.0, max_backoff_seconds=5.0)
        assert retry.backoff_for(5) == 5.0

    def test_jitter_stays_within_its_configured_band(self) -> None:
        retry = RetryPolicy(
            RetryConfig(initial_backoff_seconds=10.0, jitter_ratio=0.2),
            rng=random.Random(1234),
        )
        for _ in range(200):
            assert 8.0 <= retry.backoff_for(1) <= 12.0

    def test_zero_jitter_is_fully_deterministic(self) -> None:
        left, right = policy(), policy()
        assert [left.backoff_for(n) for n in range(1, 5)] == [
            right.backoff_for(n) for n in range(1, 5)
        ]

    def test_attempts_are_one_indexed(self) -> None:
        with pytest.raises(ValueError, match="1-indexed"):
            policy().backoff_for(0)


class TestStatusDecisions:
    @pytest.mark.parametrize("status", [429, 500, 502, 503, 504, 408])
    def test_transient_statuses_are_retried(self, status: int) -> None:
        assert policy().on_status(status, attempt=1).should_retry

    @pytest.mark.parametrize("status", [400, 401, 403, 404, 410, 422])
    def test_client_errors_are_not_retried(self, status: int) -> None:
        """Retrying an auth or not-found error is pure noise against a source."""
        decision = policy().on_status(status, attempt=1)
        assert not decision.should_retry
        assert "not retryable" in decision.reason

    def test_the_attempt_budget_is_enforced(self) -> None:
        retry = policy(max_attempts=3)
        assert retry.on_status(503, attempt=2).should_retry
        decision = retry.on_status(503, attempt=3)
        assert not decision.should_retry
        assert "exhausted" in decision.reason


class TestRetryAfter:
    def test_a_numeric_header_is_honoured(self) -> None:
        decision = policy(initial_backoff_seconds=1.0).on_status(
            429, attempt=1, headers={"retry-after": "30"}
        )
        assert decision.should_retry
        assert decision.delay_seconds == 30.0

    def test_an_http_date_header_is_honoured(self) -> None:
        from datetime import timedelta

        when = utc_now() + timedelta(seconds=45)
        decision = policy().on_status(
            503, attempt=1, headers={"retry-after": format_datetime(when)}
        )
        assert decision.should_retry
        assert 40.0 <= decision.delay_seconds <= 50.0

    def test_we_never_wait_less_than_the_server_asked(self) -> None:
        decision = policy(initial_backoff_seconds=60.0).on_status(
            429, attempt=1, headers={"retry-after": "5"}
        )
        assert decision.delay_seconds == 60.0

    def test_an_excessive_retry_after_stops_us_rather_than_blocking(self) -> None:
        """Better to fail the request than block a worker for an hour."""
        decision = policy(max_retry_after_seconds=120.0).on_status(
            429, attempt=1, headers={"retry-after": "3600"}
        )
        assert not decision.should_retry
        assert "ceiling" in decision.reason

    @pytest.mark.parametrize("value", ["", "   ", "not-a-number", "!!!"])
    def test_unparseable_headers_fall_back_to_backoff(self, value: str) -> None:
        decision = policy(initial_backoff_seconds=2.0).on_status(
            503, attempt=1, headers={"retry-after": value}
        )
        assert decision.should_retry
        assert decision.delay_seconds == 2.0

    def test_a_past_date_means_retry_immediately(self) -> None:
        from datetime import timedelta

        past = format_datetime(utc_now() - timedelta(seconds=120))
        assert parse_retry_after(past) == 0.0


class TestExceptionDecisions:
    def test_transport_errors_are_retried_within_budget(self) -> None:
        retry = policy(max_attempts=2)
        assert retry.on_exception(TimeoutError(), attempt=1).should_retry
        assert not retry.on_exception(TimeoutError(), attempt=2).should_retry

    def test_the_exception_type_is_reported(self) -> None:
        decision = policy().on_exception(ConnectionResetError(), attempt=1)
        assert "ConnectionResetError" in decision.reason


class TestTokenBucket:
    def test_burst_capacity_is_available_immediately(self) -> None:
        clock = ManualClock()
        bucket = TokenBucket(rate_per_second=1.0, capacity=3, clock=clock)
        for _ in range(3):
            assert bucket.acquire() == 0.0
        assert clock.total_slept == 0.0

    def test_beyond_the_burst_the_caller_waits(self) -> None:
        clock = ManualClock()
        bucket = TokenBucket(rate_per_second=2.0, capacity=1, clock=clock)
        bucket.acquire()
        assert bucket.acquire() == pytest.approx(0.5, abs=0.01)

    def test_the_long_run_rate_is_respected(self) -> None:
        """The property that actually keeps us inside a source's limit."""
        clock = ManualClock()
        bucket = TokenBucket(rate_per_second=1.0, capacity=1, clock=clock)
        start = clock.monotonic()
        for _ in range(10):
            bucket.acquire()
        assert clock.monotonic() - start == pytest.approx(9.0, abs=0.1)

    def test_tokens_refill_over_time(self) -> None:
        clock = ManualClock()
        bucket = TokenBucket(rate_per_second=1.0, capacity=5, clock=clock)
        for _ in range(5):
            bucket.acquire()
        clock.advance(3.0)
        assert bucket.available_tokens == pytest.approx(3.0)

    def test_refill_never_exceeds_capacity(self) -> None:
        clock = ManualClock()
        bucket = TokenBucket(rate_per_second=1.0, capacity=2, clock=clock)
        clock.advance(1000.0)
        assert bucket.available_tokens == 2.0

    def test_external_pressure_stalls_the_whole_bucket(self) -> None:
        """A 429 must slow the source, not just the one failed request."""
        clock = ManualClock()
        bucket = TokenBucket(rate_per_second=100.0, capacity=10, clock=clock)
        bucket.pause_for(30.0)
        assert bucket.time_until_available() == pytest.approx(30.0)
        bucket.acquire()
        assert clock.total_slept >= 30.0

    def test_a_non_positive_pause_is_ignored(self) -> None:
        bucket = TokenBucket(rate_per_second=1.0, capacity=1, clock=ManualClock())
        bucket.pause_for(0)
        bucket.pause_for(-5)
        assert bucket.time_until_available() == 0.0

    @pytest.mark.parametrize(("rate", "capacity"), [(0, 1), (-1, 1), (1, 0)])
    def test_invalid_configuration_is_rejected(self, rate: float, capacity: int) -> None:
        with pytest.raises(ValueError):
            TokenBucket(rate_per_second=rate, capacity=capacity, clock=ManualClock())
