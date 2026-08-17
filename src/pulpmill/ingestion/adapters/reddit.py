"""Reddit source adapter.

Acquisition method: the official OAuth2 Data API at `oauth.reddit.com`.

This is not a preference. Reddit's anonymous JSON endpoints
(`www.reddit.com/r/<sub>/hot.json`) return HTTP 403 as of 2026 regardless of
User-Agent, verified against the live host. OAuth is the only supported read
path, so the adapter implements it properly and reports itself unavailable --
with remediation -- when credentials are missing, rather than degrading into
something that scrapes HTML.

Rate limits: the documented free-tier ceiling is 100 queries/minute per OAuth
client, averaged over ten minutes. The default config requests 1 rps. The
adapter additionally reads Reddit's `X-Ratelimit-*` response headers and stalls
its own token bucket when the remaining budget runs low, so the limiter tracks
the server's view rather than only our own.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Mapping
from datetime import datetime, timedelta
from typing import Any, Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pulpmill.domain.errors import (
    IngestionError,
    SourceRequestError,
    SourceResponseError,
    SourceUnavailableError,
)
from pulpmill.domain.source import AdapterHealth, FetchRequest
from pulpmill.domain.story import Engagement, RawStory, Story
from pulpmill.infrastructure.clock import from_epoch
from pulpmill.infrastructure.http import HttpClient
from pulpmill.infrastructure.logging import get_logger
from pulpmill.ingestion.base import QUALITY_KEY, RAW_FORMAT_KEY, build_story
from pulpmill.ingestion.registry import AdapterContext, register_adapter
from pulpmill.normalization.text import clean_text
from pulpmill.normalization.url import join_permalink

PLATFORM = "reddit"

#: Bodies Reddit replaces when a post is removed. Not narratable content.
_TOMBSTONES = frozenset({"[removed]", "[deleted]", "[removed by reddit]"})

#: Refresh the token this long before it actually expires.
_TOKEN_REFRESH_MARGIN_SECONDS = 60.0


class RedditQuery(BaseModel):
    """One configured listing request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    subreddit: str
    listing: Literal["hot", "new", "top", "rising"] = "top"
    #: Only meaningful for the `top` listing; Reddit ignores it elsewhere.
    time_filter: Literal["hour", "day", "week", "month", "year", "all"] | None = None
    limit: int = Field(default=50, gt=0, le=100)


class RedditFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_score: int = 0
    min_body_chars: int = Field(default=0, ge=0)
    max_body_chars: int = Field(default=100_000, gt=0)
    allow_nsfw: bool = False
    skip_stickied: bool = True
    #: Link and image posts carry no narratable body.
    require_selftext: bool = True


class RedditOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_base_url: str = "https://oauth.reddit.com"
    oauth_token_url: str = "https://www.reddit.com/api/v1/access_token"
    permalink_base_url: str = "https://www.reddit.com"


class RedditTokenProvider:
    """Fetches and caches an OAuth access token.

    Supports both documented app-only flows:

    * `client_credentials` -- default. Read-only, needs no account password.
    * `password`           -- the "script" app grant, for when an authenticated
                              user context is required later.

    The token is refreshed slightly before expiry and never logged.
    """

    def __init__(
        self,
        *,
        http: HttpClient,
        token_url: str,
        client_id: str,
        client_secret: str,
        auth_mode: str = "client_credentials",
        username: str | None = None,
        password: str | None = None,
    ) -> None:
        self._http = http
        self._token_url = token_url
        self._client_id = client_id
        self._client_secret = client_secret
        self._auth_mode = auth_mode
        self._username = username
        self._password = password
        self._lock = threading.Lock()
        self._token: str | None = None
        self._expires_at: datetime | None = None
        self._log = get_logger("ingestion.reddit.auth", source=PLATFORM)

    def _grant_payload(self) -> dict[str, str]:
        if self._auth_mode == "password":
            if not self._username or not self._password:
                raise SourceUnavailableError(
                    "reddit auth_mode=password needs PULPMILL_REDDIT_USERNAME and "
                    "PULPMILL_REDDIT_PASSWORD",
                    source=PLATFORM,
                )
            return {
                "grant_type": "password",
                "username": self._username,
                "password": self._password,
            }
        return {"grant_type": "client_credentials"}

    def token(self, *, now: datetime) -> str:
        with self._lock:
            if self._token is not None and self._expires_at is not None and now < self._expires_at:
                return self._token

            response = self._http.request(
                "POST",
                self._token_url,
                data=self._grant_payload(),
                auth=(self._client_id, self._client_secret),
                expected_status=(200,),
            )
            try:
                payload = response.json()
            except ValueError as exc:
                raise SourceResponseError(
                    "reddit token endpoint returned a non-JSON body", source=PLATFORM
                ) from exc

            token = payload.get("access_token")
            if not isinstance(token, str) or not token:
                # Deliberately does not echo the payload: it may contain
                # credential material.
                raise SourceUnavailableError(
                    "reddit did not return an access token; check the client id and secret",
                    source=PLATFORM,
                    error=str(payload.get("error", "unknown")),
                )

            expires_in = float(payload.get("expires_in", 3600))
            self._token = token
            self._expires_at = now + timedelta(
                seconds=max(60.0, expires_in - _TOKEN_REFRESH_MARGIN_SECONDS)
            )
            self._log.info(
                "reddit_token_acquired",
                auth_mode=self._auth_mode,
                expires_in_seconds=int(expires_in),
            )
            return token

    def invalidate(self) -> None:
        with self._lock:
            self._token = None
            self._expires_at = None


class RedditAdapter:
    """Fetches and normalizes Reddit self-posts."""

    def __init__(self, context: AdapterContext) -> None:
        self._context = context
        self._clock = context.clock
        self._log = get_logger("ingestion.reddit", source=PLATFORM)

        source = context.source_config
        try:
            self._options = RedditOptions.model_validate(source.options)
            self._filters = RedditFilters.model_validate(source.filters)
            self._queries = tuple(RedditQuery.model_validate(q) for q in source.queries)
        except ValidationError as exc:
            raise IngestionError(
                f"reddit source configuration is invalid: {exc}", source=PLATFORM
            ) from exc

        self._client_id = context.secrets.get("REDDIT_CLIENT_ID")
        self._client_secret = context.secrets.get("REDDIT_CLIENT_SECRET")
        self._auth_mode = context.secrets.get("REDDIT_AUTH_MODE") or "client_credentials"
        self._username = context.secrets.get("REDDIT_USERNAME")
        self._password = context.secrets.get("REDDIT_PASSWORD")

        # Reddit mandates a unique descriptive agent in this exact format;
        # generic agents are aggressively throttled.
        self._user_agent = context.secrets.get("REDDIT_USER_AGENT") or (
            "linux:pulpmill:0.1.0 (by /u/unknown)"
        )

        transport = (
            context.transport if isinstance(context.transport, httpx.BaseTransport) else None
        )
        self._http = HttpClient(
            name=PLATFORM,
            config=context.config.http,
            rate_limit=source.rate_limit,
            user_agent=self._user_agent,
            clock=context.clock,
            rng=context.rng,
            transport=transport,
        )
        self._tokens: RedditTokenProvider | None = None

    @property
    def platform(self) -> str:
        return PLATFORM

    # --- health --------------------------------------------------------------

    def health(self) -> AdapterHealth:
        missing = [
            name
            for name, value in (
                ("PULPMILL_REDDIT_CLIENT_ID", self._client_id),
                ("PULPMILL_REDDIT_CLIENT_SECRET", self._client_secret),
            )
            if not value
        ]
        if self._auth_mode == "password":
            missing.extend(
                name
                for name, value in (
                    ("PULPMILL_REDDIT_USERNAME", self._username),
                    ("PULPMILL_REDDIT_PASSWORD", self._password),
                )
                if not value
            )
        if missing:
            return AdapterHealth(
                platform=PLATFORM,
                available=False,
                detail=f"missing credentials: {', '.join(missing)}",
                remediation=(
                    "Create a 'script' app at https://www.reddit.com/prefs/apps and put the "
                    "id and secret in .env. See docs/CREDENTIALS.md."
                ),
                metadata={"auth_mode": self._auth_mode, "queries": len(self._queries)},
            )
        if not self._queries:
            return AdapterHealth(
                platform=PLATFORM,
                available=False,
                detail="no queries configured",
                remediation="Add entries under sources.reddit.queries in config/pipeline.yaml.",
            )
        return AdapterHealth(
            platform=PLATFORM,
            available=True,
            detail="OAuth credentials present",
            metadata={
                "auth_mode": self._auth_mode,
                "queries": len(self._queries),
                "user_agent_configured": self._user_agent != "linux:pulpmill:0.1.0 (by /u/unknown)",
            },
        )

    def _token_provider(self) -> RedditTokenProvider:
        if self._tokens is None:
            if not self._client_id or not self._client_secret:
                raise SourceUnavailableError(
                    "reddit credentials are not configured", source=PLATFORM
                )
            self._tokens = RedditTokenProvider(
                http=self._http,
                token_url=self._options.oauth_token_url,
                client_id=self._client_id,
                client_secret=self._client_secret,
                auth_mode=self._auth_mode,
                username=self._username,
                password=self._password,
            )
        return self._tokens

    # --- fetch ---------------------------------------------------------------

    def fetch(self, request: FetchRequest) -> Iterator[RawStory]:
        health = self.health()
        if not health.available:
            raise SourceUnavailableError(health.detail, source=PLATFORM)

        queries = (
            tuple(RedditQuery.model_validate(q) for q in request.queries)
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
        self, query: RedditQuery, request: FetchRequest, *, remaining: int
    ) -> Iterator[RawStory]:
        url = f"{self._options.api_base_url.rstrip('/')}/r/{query.subreddit}/{query.listing}"
        after: str | None = None
        produced = 0

        for page in range(1, request.max_pages + 1):
            if produced >= remaining:
                return

            params: dict[str, Any] = {
                "limit": min(query.limit, remaining - produced, 100),
                "raw_json": 1,
            }
            if query.listing == "top" and query.time_filter:
                params["t"] = query.time_filter
            if after:
                params["after"] = after

            payload = self._authorized_get(url, params)
            listing = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(listing, dict):
                raise SourceResponseError(
                    "reddit listing response has no data object",
                    source=PLATFORM,
                    subreddit=query.subreddit,
                )

            children = listing.get("children")
            if not isinstance(children, list):
                raise SourceResponseError(
                    "reddit listing response has no children array",
                    source=PLATFORM,
                    subreddit=query.subreddit,
                )

            page_count = 0
            for child in children:
                if not isinstance(child, dict):
                    continue
                data = child.get("data")
                if not isinstance(data, dict):
                    continue

                source_id = data.get("name") or (f"t3_{data['id']}" if data.get("id") else None)
                permalink = data.get("permalink")
                if not source_id or not isinstance(permalink, str):
                    # Provenance is non-negotiable: without an id and a
                    # permalink we cannot build a traceable record, so skip it
                    # loudly rather than inventing one.
                    self._log.warning(
                        "reddit_record_missing_provenance",
                        subreddit=query.subreddit,
                        has_id=bool(source_id),
                        has_permalink=isinstance(permalink, str),
                    )
                    continue

                yield RawStory(
                    source_platform=PLATFORM,
                    source_id=str(source_id),
                    canonical_url=join_permalink(self._options.permalink_base_url, permalink),
                    fetched_at=self._clock.now(),
                    payload=data,
                    retrieval={
                        "subreddit": query.subreddit,
                        "listing": query.listing,
                        "time_filter": query.time_filter,
                        "page": page,
                    },
                )
                produced += 1
                page_count += 1
                if produced >= remaining:
                    return

            after = listing.get("after")
            if not after:
                return
            if page_count == 0 and self._context.config.ingestion.stop_on_exhausted_page:
                return

    def _authorized_get(self, url: str, params: Mapping[str, Any]) -> Any:
        """GET with a bearer token, retrying once on an expired token."""
        provider = self._token_provider()
        for attempt in (1, 2):
            token = provider.token(now=self._clock.now())
            try:
                response = self._http.request(
                    "GET",
                    url,
                    params=params,
                    headers={"Authorization": f"bearer {token}"},
                    expected_status=(200,),
                )
            except SourceRequestError as exc:
                # A 401 on a cached token means it was revoked or expired early.
                if exc.status_code == 401 and attempt == 1:
                    self._log.info("reddit_token_rejected_retrying")
                    provider.invalidate()
                    continue
                raise

            self._apply_ratelimit_headers(response.headers)
            try:
                return response.json()
            except ValueError as exc:
                raise SourceResponseError(
                    "reddit returned a non-JSON listing body", source=PLATFORM, url=url
                ) from exc
        raise SourceUnavailableError("reddit rejected the access token twice", source=PLATFORM)

    def _apply_ratelimit_headers(self, headers: Mapping[str, str]) -> None:
        """Stall our own bucket when Reddit says the budget is nearly spent.

        Reddit reports the remaining request allowance and the seconds until the
        window resets. Reacting to it is what keeps a 24/7 worker inside the
        limit instead of discovering it via 429s.
        """
        raw_remaining = headers.get("x-ratelimit-remaining")
        raw_reset = headers.get("x-ratelimit-reset")
        if raw_remaining is None or raw_reset is None:
            return
        try:
            remaining = float(raw_remaining)
            reset = float(raw_reset)
        except ValueError:
            return
        if remaining <= 2 and reset > 0:
            self._http.rate_limiter.pause_for(reset)
            self._log.warning(
                "reddit_budget_low", remaining=remaining, pause_seconds=round(reset, 1)
            )

    # --- normalize -----------------------------------------------------------

    def normalize(self, raw: RawStory) -> Story | None:
        data = raw.payload
        if not isinstance(data, Mapping):
            raise SourceResponseError("reddit payload is not an object", source=PLATFORM)

        title = str(data.get("title") or "").strip()
        if not title:
            raise SourceResponseError(
                "reddit post has no title", source=PLATFORM, source_id=raw.source_id
            )

        selftext = str(data.get("selftext") or "")
        if selftext.strip().lower() in _TOMBSTONES:
            return None
        if self._filters.require_selftext and not selftext.strip():
            return None
        if self._filters.skip_stickied and bool(data.get("stickied")):
            return None
        if not self._filters.allow_nsfw and bool(data.get("over_18")):
            return None
        if data.get("removed_by_category"):
            return None

        score = data.get("score")
        if isinstance(score, (int, float)) and int(score) < self._filters.min_score:
            return None

        body = clean_text(selftext, markdown=True)
        if len(body) < self._filters.min_body_chars:
            return None
        full_length = len(body)
        truncated = full_length > self._filters.max_body_chars
        if truncated:
            # Kept but flagged: over-length stories are exactly the ones the
            # future series splitter will cut into parts.
            body = body[: self._filters.max_body_chars]

        created_utc = data.get("created_utc")
        if not isinstance(created_utc, (int, float)):
            raise SourceResponseError(
                "reddit post has no created_utc", source=PLATFORM, source_id=raw.source_id
            )

        author = data.get("author")
        author_name = str(author) if author and str(author) != "[deleted]" else None
        subreddit = str(data.get("subreddit") or "")

        num_comments = data.get("num_comments")
        return build_story(
            platform=PLATFORM,
            source_id=raw.source_id,
            canonical_url=raw.canonical_url,
            title=title,
            raw_content=selftext,
            normalized_content=body,
            created_at=from_epoch(float(created_utc)),
            discovered_at=raw.fetched_at,
            author=author_name,
            engagement=Engagement(
                score=int(score) if isinstance(score, (int, float)) else None,
                comments=int(num_comments) if isinstance(num_comments, (int, float)) else None,
            ),
            metadata={
                QUALITY_KEY: subreddit,
                RAW_FORMAT_KEY: "markdown",
                "subreddit": subreddit,
                "permalink": str(data.get("permalink") or ""),
                "over_18": bool(data.get("over_18")),
                "spoiler": bool(data.get("spoiler")),
                "locked": bool(data.get("locked")),
                "upvote_ratio": data.get("upvote_ratio"),
                "link_flair_text": data.get("link_flair_text"),
                "truncated": truncated,
                "full_body_chars": full_length,
                "retrieval": dict(raw.retrieval),
            },
            simhash_min_tokens=self._context.config.deduplication.layers.near_duplicate.min_tokens,
        )

    def close(self) -> None:
        self._http.close()


register_adapter(PLATFORM, RedditAdapter)
