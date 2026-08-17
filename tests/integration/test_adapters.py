"""Source adapters, exercised end-to-end against recorded payloads.

An injected `httpx.MockTransport` means the real auth, pagination, filtering,
normalization and error-handling code runs -- only the socket is replaced. No
test here touches the network.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx
import pytest

from pulpmill.config.models import AppConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.domain.errors import (
    IngestionError,
    SourceRequestError,
    SourceResponseError,
    SourceUnavailableError,
)
from pulpmill.domain.source import FetchRequest
from pulpmill.ingestion.adapters.fourchan import FourchanAdapter
from pulpmill.ingestion.adapters.reddit import RedditAdapter
from pulpmill.ingestion.adapters.x import XAdapter
from pulpmill.ingestion.base import QUALITY_KEY, RAW_FORMAT_KEY
from pulpmill.ingestion.registry import AdapterContext
from tests.conftest import load_fixture
from tests.support.clock import ManualClock

Handler = Callable[[httpx.Request], httpx.Response]

REDDIT_SECRETS = SecretStore(
    environ={
        "PULPMILL_REDDIT_CLIENT_ID": "test-id",
        "PULPMILL_REDDIT_CLIENT_SECRET": "test-secret",
        "PULPMILL_REDDIT_USER_AGENT": "linux:pulpmill:test (by /u/tester)",
    }
)


def make_context(
    name: str,
    config: AppConfig,
    clock: ManualClock,
    handler: Handler,
    *,
    secrets: SecretStore | None = None,
) -> AdapterContext:
    return AdapterContext(
        name=name,
        config=config,
        source_config=config.sources[name],
        secrets=secrets or SecretStore(environ={}),
        clock=clock,
        transport=httpx.MockTransport(handler),
    )


# --- Reddit ------------------------------------------------------------------


def reddit_handler(
    *, listing_status: int = 200, body: object | None = None, calls: list[str] | None = None
) -> Handler:
    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        if request.url.path == "/api/v1/access_token":
            assert request.headers.get("Authorization", "").startswith("Basic ")
            return httpx.Response(200, json=load_fixture("reddit_token.json"))
        if listing_status != 200:
            return httpx.Response(listing_status, json={"error": listing_status})
        assert request.headers["Authorization"] == "bearer test-access-token"
        if body is not None:
            return httpx.Response(200, json=body)
        page = (
            "reddit_listing_page2.json" if "after=" in str(request.url) else "reddit_listing.json"
        )
        return httpx.Response(
            200,
            json=load_fixture(page),
            headers={"x-ratelimit-remaining": "97", "x-ratelimit-reset": "300"},
        )

    return handle


class TestRedditAdapter:
    def test_missing_credentials_report_unavailable_with_remediation(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        """Reddit's anonymous endpoints return 403, so this is the normal state."""
        adapter = RedditAdapter(make_context("reddit", config, clock, reddit_handler()))
        health = adapter.health()
        assert health.available is False
        assert "PULPMILL_REDDIT_CLIENT_ID" in health.detail
        assert health.remediation and "prefs/apps" in health.remediation
        adapter.close()

    def test_fetching_without_credentials_raises_unavailable_not_a_crash(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = RedditAdapter(make_context("reddit", config, clock, reddit_handler()))
        with pytest.raises(SourceUnavailableError):
            list(adapter.fetch(FetchRequest(queries=(), limit=5)))
        adapter.close()

    def test_credentials_present_reports_ready(self, config: AppConfig, clock: ManualClock) -> None:
        adapter = RedditAdapter(
            make_context("reddit", config, clock, reddit_handler(), secrets=REDDIT_SECRETS)
        )
        assert adapter.health().available is True
        adapter.close()

    def test_fetch_and_normalize_preserves_the_exact_permalink(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = RedditAdapter(
            make_context("reddit", config, clock, reddit_handler(), secrets=REDDIT_SECRETS)
        )
        raws = list(
            adapter.fetch(
                FetchRequest(queries=({"subreddit": "AmItheAsshole", "listing": "top"},), limit=10)
            )
        )
        story = next(s for r in raws if (s := adapter.normalize(r)) is not None)

        assert story.source_platform == "reddit"
        assert story.source_id == "t3_1abcde"
        assert story.canonical_url == (
            "https://www.reddit.com/r/AmItheAsshole/comments/1abcde/"
            "aita_for_telling_my_roommate_to_move_out/"
        )
        assert story.author == "throwaway_rent99"
        assert story.metadata[QUALITY_KEY] == "AmItheAsshole"
        assert story.metadata[RAW_FORMAT_KEY] == "markdown"
        adapter.close()

    def test_markdown_is_stripped_from_the_narratable_body(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = RedditAdapter(
            make_context("reddit", config, clock, reddit_handler(), secrets=REDDIT_SECRETS)
        )
        raw = next(iter(adapter.fetch(FetchRequest(queries=(), limit=1))))
        story = adapter.normalize(raw)
        assert story is not None
        assert "previous post" in story.normalized_content
        assert "https://example.com/old" not in story.normalized_content
        assert "**" not in story.normalized_content
        # The original markdown is retained for reproducibility.
        assert "[previous post]" in story.raw_content
        adapter.close()

    def test_engagement_is_captured(self, config: AppConfig, clock: ManualClock) -> None:
        adapter = RedditAdapter(
            make_context("reddit", config, clock, reddit_handler(), secrets=REDDIT_SECRETS)
        )
        story = adapter.normalize(next(iter(adapter.fetch(FetchRequest(queries=(), limit=1)))))
        assert story is not None
        assert story.engagement.score == 8421
        assert story.engagement.comments == 1934
        adapter.close()

    @pytest.mark.parametrize(
        ("source_id", "reason"),
        [("t3_1removd", "removed"), ("t3_1linkpo", "link-only"), ("t3_1sticky", "stickied")],
    )
    def test_unusable_posts_are_filtered_not_failed(
        self, config: AppConfig, clock: ManualClock, source_id: str, reason: str
    ) -> None:
        adapter = RedditAdapter(
            make_context("reddit", config, clock, reddit_handler(), secrets=REDDIT_SECRETS)
        )
        raws = list(adapter.fetch(FetchRequest(queries=(), limit=10)))
        target = next(raw for raw in raws if raw.source_id == source_id)
        assert adapter.normalize(target) is None, reason
        adapter.close()

    def test_pagination_follows_the_after_cursor(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        calls: list[str] = []
        adapter = RedditAdapter(
            make_context(
                "reddit", config, clock, reddit_handler(calls=calls), secrets=REDDIT_SECRETS
            )
        )
        raws = list(adapter.fetch(FetchRequest(queries=(), limit=10, max_pages=2)))
        assert any("after=" in url for url in calls)
        assert "t3_2ghijk" in {raw.source_id for raw in raws}
        adapter.close()

    def test_the_fetch_limit_is_a_hard_ceiling(self, config: AppConfig, clock: ManualClock) -> None:
        adapter = RedditAdapter(
            make_context("reddit", config, clock, reddit_handler(), secrets=REDDIT_SECRETS)
        )
        assert len(list(adapter.fetch(FetchRequest(queries=(), limit=2, max_pages=5)))) == 2
        adapter.close()

    def test_the_access_token_is_requested_once_and_reused(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        calls: list[str] = []
        adapter = RedditAdapter(
            make_context(
                "reddit", config, clock, reddit_handler(calls=calls), secrets=REDDIT_SECRETS
            )
        )
        list(adapter.fetch(FetchRequest(queries=(), limit=10, max_pages=2)))
        assert sum(1 for url in calls if "access_token" in url) == 1
        adapter.close()

    def test_a_rejected_token_is_refreshed_once(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        state = {"listing_calls": 0, "token_calls": 0}

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/access_token":
                state["token_calls"] += 1
                return httpx.Response(200, json=load_fixture("reddit_token.json"))
            state["listing_calls"] += 1
            if state["listing_calls"] == 1:
                return httpx.Response(401, json={"message": "Unauthorized"})
            return httpx.Response(200, json=load_fixture("reddit_listing.json"))

        adapter = RedditAdapter(
            make_context("reddit", config, clock, handle, secrets=REDDIT_SECRETS)
        )
        raws = list(adapter.fetch(FetchRequest(queries=(), limit=1)))
        assert raws
        assert state["token_calls"] == 2
        adapter.close()

    def test_a_low_remaining_budget_stalls_the_rate_limiter(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        """Reacting to Reddit's own headers is what keeps us inside the limit."""

        def handle(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/api/v1/access_token":
                return httpx.Response(200, json=load_fixture("reddit_token.json"))
            return httpx.Response(
                200,
                json=load_fixture("reddit_listing.json"),
                headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "45"},
            )

        adapter = RedditAdapter(
            make_context("reddit", config, clock, handle, secrets=REDDIT_SECRETS)
        )
        list(adapter.fetch(FetchRequest(queries=(), limit=1)))
        assert adapter._http.rate_limiter.time_until_available() == pytest.approx(45.0, abs=1.0)
        adapter.close()

    def test_a_persistent_server_error_surfaces_as_a_typed_failure(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = RedditAdapter(
            make_context(
                "reddit",
                config,
                clock,
                reddit_handler(listing_status=503),
                secrets=REDDIT_SECRETS,
            )
        )
        with pytest.raises(SourceRequestError) as exc:
            list(adapter.fetch(FetchRequest(queries=(), limit=1)))
        assert exc.value.status_code == 503
        assert exc.value.attempts == config.http.retry.max_attempts
        adapter.close()

    @pytest.mark.parametrize(
        "body",
        [
            {"kind": "Listing"},
            {"kind": "Listing", "data": {}},
            {"kind": "Listing", "data": {"children": "not-a-list"}},
            [],
        ],
    )
    def test_a_malformed_listing_raises_a_typed_response_error(
        self, config: AppConfig, clock: ManualClock, body: object
    ) -> None:
        adapter = RedditAdapter(
            make_context("reddit", config, clock, reddit_handler(body=body), secrets=REDDIT_SECRETS)
        )
        with pytest.raises(SourceResponseError):
            list(adapter.fetch(FetchRequest(queries=(), limit=1)))
        adapter.close()

    def test_records_without_provenance_are_skipped_not_invented(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        """Better to lose a story than to store one we cannot attribute."""
        body = {
            "kind": "Listing",
            "data": {
                "after": None,
                "children": [
                    {"kind": "t3", "data": {"title": "no id or permalink", "selftext": "x"}},
                    {"kind": "t3", "data": {"name": "t3_ok", "title": "no permalink"}},
                ],
            },
        }
        adapter = RedditAdapter(
            make_context("reddit", config, clock, reddit_handler(body=body), secrets=REDDIT_SECRETS)
        )
        assert list(adapter.fetch(FetchRequest(queries=(), limit=5))) == []
        adapter.close()

    def test_a_post_without_a_title_is_a_response_error(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        from datetime import UTC, datetime

        from pulpmill.domain.story import RawStory

        adapter = RedditAdapter(
            make_context("reddit", config, clock, reddit_handler(), secrets=REDDIT_SECRETS)
        )
        raw = RawStory(
            source_platform="reddit",
            source_id="t3_x",
            canonical_url="https://www.reddit.com/r/x/comments/x/",
            fetched_at=datetime.now(UTC),
            payload={"selftext": "body", "created_utc": 1.0},
        )
        with pytest.raises(SourceResponseError, match="title"):
            adapter.normalize(raw)
        adapter.close()

    def test_an_invalid_query_configuration_is_rejected_at_construction(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        broken = config.sources["reddit"].model_copy(
            update={"queries": ({"subreddit": "x", "listing": "not-a-listing"},)}
        )
        context = AdapterContext(
            name="reddit",
            config=config.model_copy(update={"sources": {**config.sources, "reddit": broken}}),
            source_config=broken,
            secrets=REDDIT_SECRETS,
            clock=clock,
            transport=httpx.MockTransport(reddit_handler()),
        )
        with pytest.raises(IngestionError, match="configuration is invalid"):
            RedditAdapter(context)


# --- 4chan -------------------------------------------------------------------


def _catalog_entry(thread_no: int) -> dict[str, object] | None:
    for page in load_fixture("fourchan_catalog.json"):
        for thread in page["threads"]:
            if thread["no"] == thread_no:
                return thread
    return None


def fourchan_handler(
    *,
    calls: list[str] | None = None,
    catalog_status: int = 200,
    thread_status: int = 200,
    catalog_body: object | None = None,
    last_modified: str | None = None,
) -> Handler:
    """Route like the real API: each thread URL returns *that* thread's OP."""

    def handle(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        headers = {"last-modified": last_modified} if last_modified else {}

        if request.url.path.endswith("catalog.json"):
            if catalog_status != 200:
                return httpx.Response(catalog_status)
            body = (
                catalog_body if catalog_body is not None else load_fixture("fourchan_catalog.json")
            )
            return httpx.Response(200, json=body, headers=headers)

        if thread_status != 200:
            return httpx.Response(thread_status)

        thread_no = int(request.url.path.rsplit("/", 1)[-1].removesuffix(".json"))
        op = _catalog_entry(thread_no)
        if op is None:
            return httpx.Response(404)
        return httpx.Response(
            200,
            json={"posts": [op, {"no": thread_no + 1, "com": "bump", "resto": thread_no}]},
            headers=headers,
        )

    return handle


class TestFourchanAdapter:
    def test_no_credentials_are_required(self, config: AppConfig, clock: ManualClock) -> None:
        adapter = FourchanAdapter(make_context("fourchan", config, clock, fourchan_handler()))
        health = adapter.health()
        assert health.available is True
        assert "no credentials" in health.detail
        adapter.close()

    def test_threads_normalize_with_full_provenance(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = FourchanAdapter(make_context("fourchan", config, clock, fourchan_handler()))
        raws = list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=10)))
        story = next(s for r in raws if (s := adapter.normalize(r)) is not None)

        assert story.source_platform == "fourchan"
        assert story.source_id == "x/42825148"
        assert story.canonical_url == "https://boards.4chan.org/x/thread/42825148"
        assert story.metadata["board"] == "x"
        assert story.metadata["thread_id"] == 42825148
        assert story.metadata[RAW_FORMAT_KEY] == "html"
        adapter.close()

    def test_html_is_converted_and_quotelinks_removed(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = FourchanAdapter(make_context("fourchan", config, clock, fourchan_handler()))
        story = adapter.normalize(
            next(iter(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=1))))
        )
        assert story is not None
        assert "<br>" not in story.normalized_content
        assert "quotelink" not in story.normalized_content
        assert ">>42825148" not in story.normalized_content
        # Greentext carries the story's voice and is deliberately kept.
        assert ">be me" in story.normalized_content
        # Entities are decoded.
        assert "&#039;" not in story.normalized_content
        adapter.close()

    def test_a_thread_without_a_subject_derives_a_title(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = FourchanAdapter(make_context("fourchan", config, clock, fourchan_handler()))
        raws = list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=10)))
        derived = [
            story
            for raw in raws
            if (story := adapter.normalize(raw)) is not None and story.metadata["title_derived"]
        ]
        assert derived
        assert all(story.title for story in derived)
        adapter.close()

    def test_engagement_reports_replies_and_no_score(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        """4chan has no score; recording 0 would misrepresent the platform."""
        adapter = FourchanAdapter(make_context("fourchan", config, clock, fourchan_handler()))
        story = adapter.normalize(
            next(iter(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=1))))
        )
        assert story is not None
        assert story.engagement.score is None
        assert story.engagement.comments == 87
        adapter.close()

    def test_anonymous_posts_have_no_fabricated_author(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = FourchanAdapter(make_context("fourchan", config, clock, fourchan_handler()))
        story = adapter.normalize(
            next(iter(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=1))))
        )
        assert story is not None
        assert story.author is None
        adapter.close()

    def test_a_poster_id_is_used_as_a_weak_author_identity(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = FourchanAdapter(make_context("fourchan", config, clock, fourchan_handler()))
        raws = list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=10)))
        stories = [s for raw in raws if (s := adapter.normalize(raw)) is not None]
        assert any(story.author == "ID:Ab3dEf9x" for story in stories)
        adapter.close()

    def test_stickies_and_low_reply_threads_are_filtered_before_fetching_them(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        """Filtering at the catalog stage avoids a wasted request per thread."""
        calls: list[str] = []
        adapter = FourchanAdapter(
            make_context("fourchan", config, clock, fourchan_handler(calls=calls))
        )
        raws = list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=10)))
        fetched = {raw.source_id for raw in raws}
        assert "x/42825150" not in fetched  # stickied announcement
        assert "x/42825149" not in fetched  # only one reply, below min_replies
        assert not any("42825150" in url or "42825149" in url for url in calls)
        adapter.close()

    def test_a_body_below_the_minimum_length_is_filtered_at_normalize(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        from datetime import UTC, datetime

        from pulpmill.domain.story import RawStory

        adapter = FourchanAdapter(make_context("fourchan", config, clock, fourchan_handler()))
        raw = RawStory(
            source_platform="fourchan",
            source_id="x/1",
            canonical_url="https://boards.4chan.org/x/thread/1",
            fetched_at=datetime.now(UTC),
            payload={"no": 1, "com": "too short to narrate", "time": 1785600000, "replies": 50},
            retrieval={"board": "x"},
        )
        assert adapter.normalize(raw) is None
        adapter.close()

    def test_conditional_requests_are_sent_on_a_repeat_fetch(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        """The 4chan API documentation requires If-Modified-Since."""
        seen: list[str | None] = []

        def handle(request: httpx.Request) -> httpx.Response:
            seen.append(request.headers.get("if-modified-since"))
            return httpx.Response(
                200,
                json=load_fixture("fourchan_catalog.json")
                if request.url.path.endswith("catalog.json")
                else load_fixture("fourchan_thread.json"),
                headers={"last-modified": "Sat, 01 Aug 2026 12:00:00 GMT"},
            )

        adapter = FourchanAdapter(make_context("fourchan", config, clock, handle))
        list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=1)))
        list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=1)))
        assert seen[0] is None
        assert "Sat, 01 Aug 2026" in (seen[-1] or "")
        adapter.close()

    def test_a_304_means_nothing_new_rather_than_an_error(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = FourchanAdapter(
            make_context("fourchan", config, clock, lambda request: httpx.Response(304))
        )
        assert list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=5))) == []
        adapter.close()

    def test_a_pruned_thread_falls_back_to_the_catalog_entry(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        """Threads vanish between the catalog read and the fetch. That is normal."""
        adapter = FourchanAdapter(
            make_context("fourchan", config, clock, fourchan_handler(thread_status=404))
        )
        raws = list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=2)))
        assert raws
        assert adapter.normalize(raws[0]) is not None
        adapter.close()

    def test_a_malformed_catalog_raises_a_typed_response_error(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = FourchanAdapter(
            make_context(
                "fourchan", config, clock, fourchan_handler(catalog_body={"not": "an array"})
            )
        )
        with pytest.raises(SourceResponseError, match="not an array"):
            list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=1)))
        adapter.close()

    def test_non_json_bodies_raise_a_typed_response_error(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = FourchanAdapter(
            make_context(
                "fourchan",
                config,
                clock,
                lambda request: httpx.Response(200, text="<html>maintenance</html>"),
            )
        )
        with pytest.raises(SourceResponseError, match="non-JSON"):
            list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=1)))
        adapter.close()

    def test_the_documented_one_request_per_second_limit_is_enforced(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = FourchanAdapter(make_context("fourchan", config, clock, fourchan_handler()))
        start = clock.monotonic()
        list(adapter.fetch(FetchRequest(queries=({"board": "x"},), limit=3)))
        elapsed = clock.monotonic() - start
        # Catalog + one thread fetch per accepted thread, at <= 1 rps.
        assert elapsed >= 2.0
        adapter.close()


# --- X -----------------------------------------------------------------------


class TestXAdapter:
    def test_without_a_token_it_reports_the_billing_reality(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        """Honest unavailability rather than a fake implementation."""
        adapter = XAdapter(
            make_context("x", config, clock, lambda request: httpx.Response(200, json={}))
        )
        health = adapter.health()
        assert health.available is False
        assert "no free read tier" in health.detail
        assert health.metadata["free_tier"] is False
        assert health.remediation and "docs/SOURCES.md" in health.remediation
        adapter.close()

    def test_fetching_without_a_token_raises_unavailable(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = XAdapter(
            make_context("x", config, clock, lambda request: httpx.Response(200, json={}))
        )
        with pytest.raises(SourceUnavailableError):
            list(adapter.fetch(FetchRequest(queries=(), limit=1)))
        adapter.close()

    def test_with_a_token_it_fetches_and_normalizes_real_payloads(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        captured: list[httpx.Request] = []

        def handle(request: httpx.Request) -> httpx.Response:
            captured.append(request)
            return httpx.Response(200, json=load_fixture("x_search.json"))

        adapter = XAdapter(
            make_context(
                "x",
                config,
                clock,
                handle,
                secrets=SecretStore(environ={"PULPMILL_X_BEARER_TOKEN": "bearer-test"}),
            )
        )
        assert adapter.health().available is True
        raws = list(adapter.fetch(FetchRequest(queries=({"query": "storytime"},), limit=5)))
        assert captured[0].headers["Authorization"] == "Bearer bearer-test"

        stories = [s for raw in raws if (s := adapter.normalize(raw)) is not None]
        assert len(stories) == 1  # the short post is filtered out
        story = stories[0]
        assert story.source_id == "1900000000000000001"
        assert story.canonical_url == "https://x.com/storyteller/status/1900000000000000001"
        assert story.author == "@storyteller"
        assert story.engagement.score == 5120
        assert story.engagement.comments == 128
        assert story.engagement.views == 210000
        assert story.language == "en"
        adapter.close()

    def test_an_empty_result_set_is_not_an_error(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = XAdapter(
            make_context(
                "x",
                config,
                clock,
                lambda request: httpx.Response(200, json={"meta": {"result_count": 0}}),
                secrets=SecretStore(environ={"PULPMILL_X_BEARER_TOKEN": "t"}),
            )
        )
        assert list(adapter.fetch(FetchRequest(queries=({"query": "q"},), limit=5))) == []
        adapter.close()

    def test_a_malformed_response_raises_a_typed_error(
        self, config: AppConfig, clock: ManualClock
    ) -> None:
        adapter = XAdapter(
            make_context(
                "x",
                config,
                clock,
                lambda request: httpx.Response(200, content=json.dumps([1, 2]).encode()),
                secrets=SecretStore(environ={"PULPMILL_X_BEARER_TOKEN": "t"}),
            )
        )
        with pytest.raises(SourceResponseError, match="not an object"):
            list(adapter.fetch(FetchRequest(queries=({"query": "q"},), limit=5)))
        adapter.close()
