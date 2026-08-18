"""Building the title, description and tags that accompany a video.

Deliberately spare. The description carries the source link and very little
else: a longer one would mean reproducing more of someone's post than the video
already does, and the attribution line is the part that actually matters.

Nothing here is platform-specific. Adapters apply their own limits by calling
`VideoMetadata.truncated`, which trims the body and never the attribution.
"""

from __future__ import annotations

from pulpmill.config.models import AppConfig, PublishTargetConfig
from pulpmill.domain.publishing import VideoMetadata, build_tags
from pulpmill.domain.script import NarrationScript

#: Communities where the poster is anonymous by construction and there is no
#: author to credit. Naming one anyway would be misleading.
_ANONYMOUS_PLATFORMS = frozenset({"fourchan"})


def build_metadata(
    script: NarrationScript,
    *,
    config: AppConfig,
    target: PublishTargetConfig,
) -> VideoMetadata:
    """Compose the metadata for one video on one target."""
    title = _title(script, target)
    description = _description(script, config=config, target=target)
    return VideoMetadata(
        title=title,
        description=description,
        tags=build_tags(target.hashtags),
        privacy=target.privacy,
        source_url=script.provenance.canonical_url,
        provenance=script.provenance,
        extra={"part_number": script.part_number, "total_parts": script.total_parts},
    ).truncated(
        title_max=target.title_max_chars,
        description_max=target.description_max_chars,
    )


def _title(script: NarrationScript, target: PublishTargetConfig) -> str:
    """Title, with the part label when it matters.

    The label goes at the *front* for a series. A viewer scrolling a channel
    needs to know this is part three before they need to know its subject, and
    a title truncated by the platform loses its tail first.
    """
    base = script.title.strip() or script.provenance.title.strip()
    if script.is_series:
        base = f"[Part {script.part_number}/{script.total_parts}] {base}"

    tag = next((f"#{item.lstrip('#')}" for item in target.hashtags), "")
    if tag and len(base) + len(tag) + 1 <= target.title_max_chars:
        return f"{base} {tag}"
    return base


def _description(script: NarrationScript, *, config: AppConfig, target: PublishTargetConfig) -> str:
    community = str(script.metadata.get("community") or script.provenance.source_platform)
    lines = [script.title.strip()]

    if script.is_series:
        lines.append(f"Part {script.part_number} of {script.total_parts}.")

    if script.provenance.source_platform in _ANONYMOUS_PLATFORMS:
        lines.append(f"Posted anonymously to /{community}/.")
    elif script.provenance.author:
        lines.append(f"Originally posted by u/{script.provenance.author} in r/{community}.")
    else:
        lines.append(f"Originally posted in r/{community}.")

    hashtags = " ".join(f"#{tag}" for tag in build_tags(target.hashtags))
    if hashtags:
        lines.append(hashtags)

    attribution = config.publishing.attribution_template.format(url=script.provenance.canonical_url)
    return "\n\n".join([*[line for line in lines if line], attribution])
