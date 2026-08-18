"""Publishing value objects.

Deliberately platform-agnostic. YouTube wants a `snippet` and a `status`,
Instagram wants a container id and a public URL, TikTok wants a source-info
envelope; none of that appears here. What appears here is what every platform
genuinely shares: a file, some metadata, a privacy setting, and an outcome.

The differences live in the adapters, which is the same arrangement that keeps
Reddit's and 4chan's incompatible metadata semantics out of the Story model.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from pulpmill.domain.story import Provenance


class PublishState(StrEnum):
    """Lifecycle of one attempt to publish one video to one platform."""

    PENDING = "PENDING"
    UPLOADING = "UPLOADING"
    PUBLISHED = "PUBLISHED"
    FAILED = "FAILED"
    #: Deliberately not attempted -- quota reached, target disabled, dry run.
    SKIPPED = "SKIPPED"


@dataclass(frozen=True, slots=True)
class VideoMetadata:
    """What accompanies the file, before any platform-specific shaping."""

    title: str
    description: str
    tags: tuple[str, ...]
    privacy: str
    #: The source URL. Always present, always in the description: attribution is
    #: not permission, but it makes a takedown a conversation rather than a
    #: strike.
    source_url: str
    provenance: Provenance
    extra: Mapping[str, Any] = field(default_factory=dict)

    def truncated(self, *, title_max: int, description_max: int) -> VideoMetadata:
        """Fit platform limits without losing the attribution line.

        The description is trimmed from the *front* portion, never the tail, so
        the source link survives every limit.
        """
        title = self.title if len(self.title) <= title_max else _clip(self.title, title_max)
        description = self.description
        if len(description) > description_max:
            attribution = description.rsplit("\n\n", 1)[-1]
            budget = description_max - len(attribution) - 2
            head = _clip(description[:budget], max(budget, 0)) if budget > 0 else ""
            description = f"{head}\n\n{attribution}" if head else attribution[:description_max]
        return VideoMetadata(
            title=title,
            description=description,
            tags=self.tags,
            privacy=self.privacy,
            source_url=self.source_url,
            provenance=self.provenance,
            extra=self.extra,
        )


def _clip(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    cut = text[: limit - 1].rsplit(" ", 1)[0]
    return f"{cut}…" if cut else text[:limit]


@dataclass(frozen=True, slots=True)
class PublishRequest:
    """Everything an adapter needs, and nothing it does not.

    Credentials are explicitly absent: an adapter resolves its own from the
    secret store. Keeping them out of the request is what lets the whole request
    be persisted and logged without redaction.
    """

    video_path: Path
    metadata: VideoMetadata
    story_id: str
    script_id: str
    video_id: str
    dry_run: bool

    def to_record(self) -> dict[str, Any]:
        """The persistable view. Contains no credentials by construction."""
        return {
            "title": self.metadata.title,
            "description": self.metadata.description,
            "tags": list(self.metadata.tags),
            "privacy": self.metadata.privacy,
            "source_url": self.metadata.source_url,
            "file": self.video_path.name,
            "dry_run": self.dry_run,
        }


@dataclass(frozen=True, slots=True)
class PublishResult:
    target: str
    state: PublishState
    remote_id: str | None = None
    remote_url: str | None = None
    detail: str = ""
    response: Mapping[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.state is PublishState.PUBLISHED


@dataclass(frozen=True, slots=True)
class PublisherHealth:
    """Whether a target can publish right now, and what to do if it cannot."""

    available: bool
    detail: str
    #: Concrete next step for an operator. Every platform here has an approval
    #: gate that no amount of retrying will clear, so "what to do" matters more
    #: than "what failed".
    remediation: str | None = None


def build_tags(hashtags: Sequence[str], *, limit: int = 15) -> tuple[str, ...]:
    """Normalise hashtags to bare tags, de-duplicated, order preserved."""
    seen: dict[str, None] = {}
    for tag in hashtags:
        cleaned = tag.strip().lstrip("#").strip()
        if cleaned:
            seen.setdefault(cleaned.lower(), None)
    return tuple(list(seen)[:limit])
