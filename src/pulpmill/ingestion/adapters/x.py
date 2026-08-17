"""X (Twitter) source adapter.

**Read this before enabling the source.**

Investigation outcome (August 2026): there is no free, stable, appropriate
acquisition path for X content.

* The free read tier was discontinued. As of 6 February 2026 X moved new
  developers to pay-per-use billing at roughly $0.005 per post read, with no
  free allowance to prototype against. The legacy Basic ($200/mo) and Pro
  ($5 000/mo) tiers are closed to new signups.
* The remaining supported interface is the official API v2 recent-search
  endpoint with an OAuth 2.0 App-Only bearer token. That is what this adapter
  implements.
* Scraping x.com HTML, using undocumented internal GraphQL endpoints, or
  routing through logged-in session cookies would all mean working around
  authentication and anti-bot controls. Those are not implemented here.

So the adapter is real -- it speaks the documented API and normalizes real
responses -- but it ships **disabled** in `config/pipeline.yaml`, and without a
bearer token `health()` reports unavailable with the cost caveat instead of
pretending the source works. Enabling it is a billing decision, not a technical
one.

Also note that a tweet is a poor fit for narration: the recent-search endpoint
returns posts, not threads, and the useful long-form content on X lives in
multi-post threads that recent search does not assemble. The default query and
the 240-character minimum reflect that. See docs/SOURCES.md.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pulpmill.domain.errors import IngestionError, SourceResponseError, SourceUnavailableError
from pulpmill.domain.source import AdapterHealth, FetchRequest
from pulpmill.domain.story import Engagement, RawStory, Story
from pulpmill.infrastructure.clock import from_iso
from pulpmill.infrastructure.http import HttpClient
from pulpmill.infrastructure.logging import get_logger
from pulpmill.ingestion.base import QUALITY_KEY, RAW_FORMAT_KEY, build_story
from pulpmill.ingestion.registry import AdapterContext, register_adapter
from pulpmill.normalization.text import clean_text

PLATFORM = "x"

_TWEET_FIELDS = "created_at,public_metrics,lang,author_id,conversation_id,possibly_sensitive"
_USER_FIELDS = "username,name,verified"


class XQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    #: API v2 recent search accepts 10-100 results per page.
    max_results: int = Field(default=50, ge=10, le=100)


class XFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_body_chars: int = Field(default=0, ge=0)
    max_body_chars: int = Field(default=100_000, gt=0)
    allow_sensitive: bool = False


class XOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_base_url: str = "https://api.x.com/2"
    web_base_url: str = "https://x.com"


class XAdapter:
    """Fetches posts via X API v2 recent search."""

    def __init__(self, context: AdapterContext) -> None:
        self._context = context
        self._clock = context.clock
        self._log = get_logger("ingestion.x", source=PLATFORM)

        source = context.source_config
        try:
            self._options = XOptions.model_validate(source.options)
            self._filters = XFilters.model_validate(source.filters)
            self._queries = tuple(XQuery.model_validate(q) for q in source.queries)
        except ValidationError as exc:
            raise IngestionError(
                f"x source configuration is invalid: {exc}", source=PLATFORM
            ) from exc

        self._bearer_token = context.secrets.get("X_BEARER_TOKEN")

        transport = (
            context.transport if isinstance(context.transport, httpx.BaseTransport) else None
        )
        self._http = HttpClient(
            name=PLATFORM,
            config=context.config.http,
            rate_limit=source.rate_limit,
            user_agent=context.config.http.user_agent,
            clock=context.clock,
            rng=context.rng,
            transport=transport,
        )

    @property
    def platform(self) -> str:
        return PLATFORM

    def health(self) -> AdapterHealth:
        if not self._bearer_token:
            return AdapterHealth(
                platform=PLATFORM,
                available=False,
                detail=(
                    "no bearer token, and X has no free read tier: reads are billed "
                    "per request (~$0.005/post as of Feb 2026)"
                ),
                remediation=(
                    "Set PULPMILL_X_BEARER_TOKEN from an X developer project, and set "
                    "sources.x.enabled: true. Read docs/SOURCES.md first -- this costs money."
                ),
                metadata={"billing": "pay-per-use", "free_tier": False},
            )
        if not self._queries:
            return AdapterHealth(
                platform=PLATFORM,
                available=False,
                detail="no queries configured",
                remediation="Add entries under sources.x.queries in config/pipeline.yaml.",
            )
        return AdapterHealth(
            platform=PLATFORM,
            available=True,
            detail="bearer token present -- note that every request is billed",
            metadata={"queries": len(self._queries), "billing": "pay-per-use"},
        )

    # --- fetch ---------------------------------------------------------------

    def fetch(self, request: FetchRequest) -> Iterator[RawStory]:
        health = self.health()
        if not health.available:
            raise SourceUnavailableError(health.detail, source=PLATFORM)

        queries = (
            tuple(XQuery.model_validate(q) for q in request.queries)
            if request.queries
            else self._queries
        )
        emitted = 0
        for query in queries:
            if emitted >= request.limit:
                return
            for raw in self._fetch_query(query, request, remaining=request.limit - emitted):
                yield raw
                emitted += 1
                if emitted >= request.limit:
                    return

    def _fetch_query(
        self, query: XQuery, request: FetchRequest, *, remaining: int
    ) -> Iterator[RawStory]:
        url = f"{self._options.api_base_url.rstrip('/')}/tweets/search/recent"
        next_token: str | None = None
        produced = 0

        for page in range(1, request.max_pages + 1):
            if produced >= remaining:
                return

            params: dict[str, Any] = {
                "query": query.query,
                "max_results": max(10, min(query.max_results, 100)),
                "tweet.fields": _TWEET_FIELDS,
                "expansions": "author_id",
                "user.fields": _USER_FIELDS,
            }
            if next_token:
                params["next_token"] = next_token

            payload = self._http.get_json(
                url,
                params=params,
                headers={"Authorization": f"Bearer {self._bearer_token}"},
            )
            if not isinstance(payload, dict):
                raise SourceResponseError("x response is not an object", source=PLATFORM)

            posts = payload.get("data")
            if not isinstance(posts, list):
                # An empty result set is legitimate: recent search covers only
                # the last 7 days and a narrow query may match nothing.
                self._log.info("x_no_results", query=query.query, page=page)
                return

            users = _index_users(payload.get("includes"))

            for post in posts:
                if produced >= remaining:
                    return
                if not isinstance(post, dict):
                    continue
                post_id = post.get("id")
                if not isinstance(post_id, str) or not post_id:
                    self._log.warning("x_post_missing_id", query=query.query)
                    continue

                author = users.get(str(post.get("author_id") or ""))
                username = author.get("username") if author else None
                canonical = (
                    f"{self._options.web_base_url.rstrip('/')}/{username}/status/{post_id}"
                    if username
                    else f"{self._options.web_base_url.rstrip('/')}/i/status/{post_id}"
                )

                yield RawStory(
                    source_platform=PLATFORM,
                    source_id=post_id,
                    canonical_url=canonical,
                    fetched_at=self._clock.now(),
                    payload={"post": post, "author": author or {}},
                    retrieval={
                        "query": query.query,
                        "page": page,
                        "endpoint": "tweets/search/recent",
                    },
                )
                produced += 1

            meta = payload.get("meta")
            next_token = meta.get("next_token") if isinstance(meta, dict) else None
            if not next_token:
                return

    # --- normalize -----------------------------------------------------------

    def normalize(self, raw: RawStory) -> Story | None:
        payload = raw.payload
        if not isinstance(payload, Mapping):
            raise SourceResponseError("x payload is not an object", source=PLATFORM)
        post = payload.get("post")
        if not isinstance(post, Mapping):
            raise SourceResponseError("x payload has no post object", source=PLATFORM)

        if not self._filters.allow_sensitive and bool(post.get("possibly_sensitive")):
            return None

        raw_text = str(post.get("text") or "")
        body = clean_text(raw_text)
        if len(body) < self._filters.min_body_chars:
            return None
        full_length = len(body)
        truncated = full_length > self._filters.max_body_chars
        if truncated:
            body = body[: self._filters.max_body_chars]

        created_at = post.get("created_at")
        if not isinstance(created_at, str) or not created_at:
            raise SourceResponseError(
                "x post has no created_at", source=PLATFORM, source_id=raw.source_id
            )

        metrics = post.get("public_metrics")
        metrics = metrics if isinstance(metrics, Mapping) else {}
        author = payload.get("author")
        author = author if isinstance(author, Mapping) else {}
        username = author.get("username")

        return build_story(
            platform=PLATFORM,
            source_id=raw.source_id,
            canonical_url=raw.canonical_url,
            # A post has no title field; the opening line is the honest stand-in.
            title=_derive_title(body),
            raw_content=raw_text,
            normalized_content=body,
            created_at=from_iso(created_at),
            discovered_at=raw.fetched_at,
            author=f"@{username}" if username else None,
            language=str(post.get("lang")) if post.get("lang") else None,
            engagement=Engagement(
                score=_as_int(metrics.get("like_count")),
                comments=_as_int(metrics.get("reply_count")),
                reactions=_as_int(metrics.get("quote_count")),
                shares=_as_int(metrics.get("retweet_count")),
                views=_as_int(metrics.get("impression_count")),
            ),
            metadata={
                QUALITY_KEY: f"@{username}" if username else "",
                RAW_FORMAT_KEY: "plain",
                "username": username,
                "author_id": post.get("author_id"),
                "conversation_id": post.get("conversation_id"),
                "possibly_sensitive": bool(post.get("possibly_sensitive")),
                "title_derived": True,
                "truncated": truncated,
                "full_body_chars": full_length,
                "retrieval": dict(raw.retrieval),
            },
            simhash_min_tokens=self._context.config.deduplication.layers.near_duplicate.min_tokens,
        )

    def close(self) -> None:
        self._http.close()


def _index_users(includes: Any) -> dict[str, Mapping[str, Any]]:
    """Build an author_id -> user lookup from the `includes` expansion."""
    if not isinstance(includes, Mapping):
        return {}
    users = includes.get("users")
    if not isinstance(users, list):
        return {}
    indexed: dict[str, Mapping[str, Any]] = {}
    for user in users:
        if isinstance(user, Mapping):
            user_id = user.get("id")
            if isinstance(user_id, str):
                indexed[user_id] = user
    return indexed


def _as_int(value: Any) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None


def _derive_title(body: str, *, max_chars: int = 110) -> str:
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if not first_line:
        return "Untitled post"
    if len(first_line) <= max_chars:
        return first_line
    cut = first_line[:max_chars].rsplit(" ", 1)[0]
    return f"{cut}..."


register_adapter(PLATFORM, XAdapter)
