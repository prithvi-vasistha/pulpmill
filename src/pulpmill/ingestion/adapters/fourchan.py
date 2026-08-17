"""4chan source adapter.

Acquisition method: the official read-only JSON API at `a.4cdn.org`, documented
at https://github.com/4chan/4chan-API. No authentication, no account, no
scraping of the HTML site. Verified reachable and returning HTTP 200 during
development.

The documentation states three hard rules, all honoured here:

* no more than one request per second  -> enforced by the token bucket
  (`sources.fourchan.rate_limit`, default 1 rps)
* send `If-Modified-Since`             -> sent per URL once a `Last-Modified` is
  known, and a `304` is treated as "nothing new"
* only GET/HEAD/OPTIONS                -> only GET is used

4chan's metadata semantics differ from Reddit's and are not forced into the same
shape: there is no score, posts are usually anonymous, and "engagement" means
replies and images. The engagement model carries `None` for what the platform
does not report, so ranking drops that axis instead of scoring it zero.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from pulpmill.domain.errors import IngestionError, SourceResponseError
from pulpmill.domain.source import AdapterHealth, FetchRequest
from pulpmill.domain.story import Engagement, RawStory, Story
from pulpmill.infrastructure.clock import from_epoch
from pulpmill.infrastructure.http import HttpClient
from pulpmill.infrastructure.logging import get_logger
from pulpmill.ingestion.base import QUALITY_KEY, RAW_FORMAT_KEY, build_story
from pulpmill.ingestion.registry import AdapterContext, register_adapter
from pulpmill.normalization.text import clean_text

PLATFORM = "fourchan"

#: 4chan renders cross-post references as `<a class="quotelink">&gt;&gt;123</a>`.
#: The whole element goes, because a bare ">>123" reads as noise in narration.
#: Greentext (`<span class="quote">&gt;text</span>`) is deliberately kept -- it
#: carries the story's voice.
_QUOTELINK_ANCHOR = re.compile(
    r"<a\b[^>]*\bclass=\"quotelink\"[^>]*>.*?</a>", re.IGNORECASE | re.DOTALL
)
#: Any reference that survived because it was not wrapped in an anchor.
_BARE_QUOTELINK = re.compile(r">>\d+(?:\s*\(OP\))?")


class FourchanQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    board: str
    max_threads: int = Field(default=40, gt=0, le=200)


class FourchanFilters(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    min_body_chars: int = Field(default=0, ge=0)
    max_body_chars: int = Field(default=100_000, gt=0)
    min_replies: int = Field(default=0, ge=0)
    skip_sticky: bool = True


class FourchanOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    api_base_url: str = "https://a.4cdn.org"
    web_base_url: str = "https://boards.4chan.org"
    #: Catalog OP text can be abbreviated; fetching the thread gives the
    #: authoritative body at the cost of one extra request per accepted thread.
    fetch_full_thread: bool = True


class FourchanAdapter:
    """Fetches OP posts from board catalogs and normalizes them into stories."""

    def __init__(self, context: AdapterContext) -> None:
        self._context = context
        self._clock = context.clock
        self._log = get_logger("ingestion.fourchan", source=PLATFORM)

        source = context.source_config
        try:
            self._options = FourchanOptions.model_validate(source.options)
            self._filters = FourchanFilters.model_validate(source.filters)
            self._queries = tuple(FourchanQuery.model_validate(q) for q in source.queries)
        except ValidationError as exc:
            raise IngestionError(
                f"fourchan source configuration is invalid: {exc}", source=PLATFORM
            ) from exc

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
        #: Last-Modified values per URL, so repeated fetches in one process can
        #: send If-Modified-Since as the API documentation requires.
        self._last_modified: dict[str, str] = {}

    @property
    def platform(self) -> str:
        return PLATFORM

    def health(self) -> AdapterHealth:
        if not self._queries:
            return AdapterHealth(
                platform=PLATFORM,
                available=False,
                detail="no boards configured",
                remediation="Add entries under sources.fourchan.queries in config/pipeline.yaml.",
            )
        return AdapterHealth(
            platform=PLATFORM,
            available=True,
            detail="public read-only API, no credentials required",
            metadata={
                "boards": [query.board for query in self._queries],
                "fetch_full_thread": self._options.fetch_full_thread,
            },
        )

    # --- fetch ---------------------------------------------------------------

    def fetch(self, request: FetchRequest) -> Iterator[RawStory]:
        queries = (
            tuple(FourchanQuery.model_validate(q) for q in request.queries)
            if request.queries
            else self._queries
        )
        emitted = 0
        for query in queries:
            if emitted >= request.limit:
                return
            for raw in self._fetch_board(query, remaining=request.limit - emitted):
                yield raw
                emitted += 1
                if emitted >= request.limit:
                    return

    def _get_json(self, url: str) -> Any | None:
        """GET with conditional-request support. Returns None on `304`."""
        headers: dict[str, str] = {}
        cached = self._last_modified.get(url)
        if cached:
            headers["If-Modified-Since"] = cached

        response = self._http.request(
            "GET", url, headers=headers or None, expected_status=(200, 304)
        )
        if response.status_code == 304:
            self._log.debug("fourchan_not_modified", url=url)
            return None

        last_modified = response.headers.get("last-modified")
        if last_modified:
            self._last_modified[url] = last_modified
        try:
            return response.json()
        except ValueError as exc:
            raise SourceResponseError(
                "4chan returned a non-JSON body", source=PLATFORM, url=url
            ) from exc

    def _fetch_board(self, query: FourchanQuery, *, remaining: int) -> Iterator[RawStory]:
        catalog_url = f"{self._options.api_base_url.rstrip('/')}/{query.board}/catalog.json"
        payload = self._get_json(catalog_url)
        if payload is None:
            return
        if not isinstance(payload, list):
            raise SourceResponseError(
                "4chan catalog is not an array", source=PLATFORM, board=query.board
            )

        produced = 0
        for page in payload:
            if produced >= min(remaining, query.max_threads):
                return
            if not isinstance(page, dict):
                continue
            threads = page.get("threads")
            if not isinstance(threads, list):
                continue

            for thread in threads:
                if produced >= min(remaining, query.max_threads):
                    return
                if not isinstance(thread, dict):
                    continue

                thread_no = thread.get("no")
                if not isinstance(thread_no, int):
                    self._log.warning("fourchan_thread_missing_no", board=query.board)
                    continue
                if self._filters.skip_sticky and thread.get("sticky"):
                    continue
                replies = thread.get("replies")
                if isinstance(replies, int) and replies < self._filters.min_replies:
                    continue

                payload_data: dict[str, Any] = dict(thread)
                if self._options.fetch_full_thread:
                    op = self._fetch_thread_op(query.board, thread_no)
                    if op is not None:
                        # Catalog counters (replies/images) are authoritative;
                        # the thread OP has the authoritative body.
                        merged = dict(op)
                        merged["replies"] = thread.get("replies", op.get("replies"))
                        merged["images"] = thread.get("images", op.get("images"))
                        payload_data = merged

                yield RawStory(
                    source_platform=PLATFORM,
                    source_id=f"{query.board}/{thread_no}",
                    canonical_url=(
                        f"{self._options.web_base_url.rstrip('/')}/{query.board}/thread/{thread_no}"
                    ),
                    fetched_at=self._clock.now(),
                    payload=payload_data,
                    retrieval={
                        "board": query.board,
                        "page": page.get("page"),
                        "endpoint": "catalog.json",
                        "full_thread_fetched": self._options.fetch_full_thread,
                    },
                )
                produced += 1

    def _fetch_thread_op(self, board: str, thread_no: int) -> Mapping[str, Any] | None:
        """Fetch a thread and return its OP post.

        A thread that 404s has been pruned between the catalog read and now,
        which is completely normal on 4chan -- log it and fall back to the
        catalog entry rather than failing the run.
        """
        url = f"{self._options.api_base_url.rstrip('/')}/{board}/thread/{thread_no}.json"
        try:
            payload = self._get_json(url)
        except IngestionError as exc:
            self._log.info(
                "fourchan_thread_unavailable",
                board=board,
                thread_no=thread_no,
                reason=str(exc),
            )
            return None
        if payload is None:
            return None
        if not isinstance(payload, dict):
            return None
        posts = payload.get("posts")
        if not isinstance(posts, list) or not posts or not isinstance(posts[0], dict):
            return None
        return posts[0]

    # --- normalize -----------------------------------------------------------

    def normalize(self, raw: RawStory) -> Story | None:
        data = raw.payload
        if not isinstance(data, Mapping):
            raise SourceResponseError("4chan payload is not an object", source=PLATFORM)

        board = str(raw.retrieval.get("board") or "")
        thread_no = data.get("no")
        if not isinstance(thread_no, int):
            raise SourceResponseError(
                "4chan post has no thread number", source=PLATFORM, source_id=raw.source_id
            )

        raw_comment = str(data.get("com") or "")
        if not raw_comment.strip():
            return None

        # Drop quotelink anchors while the markup is intact, then sweep up any
        # bare references left in the plain text.
        body = clean_text(_QUOTELINK_ANCHOR.sub("", raw_comment), html_source=True)
        body = clean_text(_BARE_QUOTELINK.sub("", body))

        if len(body) < self._filters.min_body_chars:
            return None
        full_length = len(body)
        truncated = full_length > self._filters.max_body_chars
        if truncated:
            body = body[: self._filters.max_body_chars]

        # 4chan threads often have no subject line; the first sentence of the OP
        # is the honest stand-in, and the real subject is kept in metadata.
        subject = clean_text(str(data.get("sub") or ""), html_source=True)
        title = subject or _derive_title(body)
        if not title:
            return None

        timestamp = data.get("time")
        if not isinstance(timestamp, (int, float)):
            raise SourceResponseError(
                "4chan post has no timestamp", source=PLATFORM, source_id=raw.source_id
            )

        replies = data.get("replies")
        images = data.get("images")

        return build_story(
            platform=PLATFORM,
            source_id=raw.source_id,
            canonical_url=raw.canonical_url,
            title=title,
            raw_content=raw_comment,
            normalized_content=body,
            created_at=from_epoch(float(timestamp)),
            discovered_at=raw.fetched_at,
            author=_derive_author(data),
            engagement=Engagement(
                # No score exists on this platform. Left as None deliberately.
                score=None,
                comments=int(replies) if isinstance(replies, int) else None,
                reactions=int(images) if isinstance(images, int) else None,
            ),
            metadata={
                QUALITY_KEY: board,
                RAW_FORMAT_KEY: "html",
                "board": board,
                "thread_id": thread_no,
                "post_id": thread_no,
                "subject": subject,
                "title_derived": not subject,
                "poster_id": data.get("id"),
                "tripcode": data.get("trip"),
                "images": images,
                "sticky": bool(data.get("sticky")),
                "closed": bool(data.get("closed")),
                "semantic_url": data.get("semantic_url"),
                "truncated": truncated,
                "full_body_chars": full_length,
                "retrieval": dict(raw.retrieval),
            },
            simhash_min_tokens=self._context.config.deduplication.layers.near_duplicate.min_tokens,
        )

    def close(self) -> None:
        self._http.close()


def _derive_title(body: str, *, max_chars: int = 110) -> str:
    """Use the opening sentence as a title when the thread has no subject."""
    first_line = next((line.strip() for line in body.splitlines() if line.strip()), "")
    if not first_line:
        return ""
    for terminator in (". ", "? ", "! "):
        index = first_line.find(terminator)
        if 0 < index <= max_chars:
            return first_line[: index + 1].strip()
    if len(first_line) <= max_chars:
        return first_line
    cut = first_line[:max_chars].rsplit(" ", 1)[0]
    return f"{cut}..."


def _derive_author(data: Mapping[str, Any]) -> str | None:
    """Best available author identity.

    4chan is anonymous by design. A tripcode is a real persistent identity; a
    per-thread poster ID is a weaker one. Plain "Anonymous" carries no
    information, so it is stored as no author rather than as a fake one.
    """
    trip = data.get("trip")
    if isinstance(trip, str) and trip.strip():
        name = str(data.get("name") or "Anonymous").strip()
        return f"{name}{trip}"
    poster_id = data.get("id")
    if isinstance(poster_id, str) and poster_id.strip() and poster_id != "Heaven":
        return f"ID:{poster_id}"
    name = str(data.get("name") or "").strip()
    if name and name.lower() != "anonymous":
        return name
    return None


register_adapter(PLATFORM, FourchanAdapter)
