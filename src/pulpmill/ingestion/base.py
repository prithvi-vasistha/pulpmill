"""Shared adapter plumbing.

`build_story` centralises everything that must be identical across sources --
id derivation, fingerprints, hashes, word count, initial status. An adapter's
job is reduced to answering platform-specific questions: where does the text
live, what is the canonical URL, what counts as engagement here.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any

from pulpmill.domain.enums import StoryStatus
from pulpmill.domain.story import Engagement, Story, build_story_id
from pulpmill.normalization.hashing import content_hash, simhash64, url_fingerprint
from pulpmill.normalization.text import count_words, tokenize
from pulpmill.normalization.url import canonical_url as canonicalise

#: Metadata key every adapter sets so the source-quality ranking signal can look
#: up a per-community weight without knowing what a subreddit or a board is.
QUALITY_KEY = "quality_key"

#: Metadata key recording how `raw_content` is encoded, so text can be
#: re-normalized later without anyone asking "which platform was this?".
#: Values: "markdown", "html", "plain".
RAW_FORMAT_KEY = "raw_format"


def build_story(
    *,
    platform: str,
    source_id: str,
    canonical_url: str,
    title: str,
    raw_content: str,
    normalized_content: str,
    created_at: datetime,
    discovered_at: datetime,
    engagement: Engagement,
    metadata: Mapping[str, Any],
    author: str | None = None,
    language: str | None = None,
    simhash_min_tokens: int = 40,
) -> Story:
    """Assemble a canonical `Story` from already-cleaned adapter output.

    The SimHash is omitted for short bodies: below roughly 40 tokens the
    fingerprint is dominated by a handful of words and starts producing false
    near-duplicate matches, which is worse than having no layer-4 signal.
    """
    if not title.strip():
        raise ValueError("story title cannot be empty")

    tokens = tokenize(normalized_content)
    fingerprint = simhash64(tokens) if len(tokens) >= simhash_min_tokens else None
    stored_url = canonicalise(canonical_url)

    return Story(
        id=build_story_id(platform, source_id),
        source_platform=platform,
        source_id=source_id,
        canonical_url=stored_url,
        url_fingerprint=url_fingerprint(stored_url),
        author=author,
        title=title.strip(),
        raw_content=raw_content,
        normalized_content=normalized_content,
        content_hash=content_hash(normalized_content),
        simhash=fingerprint,
        word_count=count_words(normalized_content),
        language=language,
        created_at=created_at,
        discovered_at=discovered_at,
        updated_at=discovered_at,
        engagement=engagement,
        metadata=dict(metadata),
        status=StoryStatus.DISCOVERED,
    )
