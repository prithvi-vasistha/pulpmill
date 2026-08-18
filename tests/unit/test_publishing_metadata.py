"""Publishing metadata: titles, descriptions and attribution."""

from __future__ import annotations

from datetime import UTC, datetime

from pulpmill.config.models import AppConfig, PublishTargetConfig
from pulpmill.domain.publishing import PublishState, VideoMetadata, build_tags
from pulpmill.domain.script import LineRole, NarrationScript, ScriptLine, build_script_id
from pulpmill.domain.story import Provenance
from pulpmill.publishing.base import available_publishers, redacted
from pulpmill.publishing.metadata import build_metadata

PROVENANCE = Provenance(
    source_platform="reddit",
    source_id="abc123",
    canonical_url="https://www.reddit.com/r/AmItheAsshole/comments/abc123/x/",
    author="throwaway99",
    title="AITA for leaving early?",
)


def make_script(**overrides: object) -> NarrationScript:
    defaults: dict[str, object] = {
        "id": build_script_id("story-1", 1),
        "story_id": "story-1",
        "part_number": 1,
        "total_parts": 1,
        "series_id": None,
        "part_id": None,
        "provenance": PROVENANCE,
        "title": "AITA for leaving early?",
        "lines": (
            ScriptLine(index=0, role=LineRole.HOOK, text="Hook.", speech_text="Hook."),
            ScriptLine(index=1, role=LineRole.BODY, text="Body.", speech_text="Body."),
        ),
        "generator": "deterministic",
        "generator_version": "test",
        "config_fingerprint": "abc",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
        "metadata": {"community": "AmItheAsshole"},
    }
    defaults.update(overrides)
    return NarrationScript(**defaults)  # type: ignore[arg-type]


def target(**overrides: object) -> PublishTargetConfig:
    defaults: dict[str, object] = {
        "adapter": "youtube",
        "hashtags": ("#shorts", "#storytime"),
        "title_max_chars": 100,
        "description_max_chars": 4900,
    }
    defaults.update(overrides)
    return PublishTargetConfig(**defaults)  # type: ignore[arg-type]


class TestTitles:
    def test_a_hashtag_is_appended_when_it_fits(self, config: AppConfig) -> None:
        meta = build_metadata(make_script(), config=config, target=target())
        assert meta.title.endswith("#shorts")

    def test_a_series_part_is_labelled_at_the_front(self, config: AppConfig) -> None:
        """A truncated title loses its tail, so the part number goes first."""
        meta = build_metadata(
            make_script(part_number=2, total_parts=4), config=config, target=target()
        )
        assert meta.title.startswith("[Part 2/4]")

    def test_the_title_respects_the_platform_limit(self, config: AppConfig) -> None:
        meta = build_metadata(
            make_script(title="x" * 300), config=config, target=target(title_max_chars=60)
        )
        assert len(meta.title) <= 60


class TestDescriptions:
    def test_the_source_url_is_always_present(self, config: AppConfig) -> None:
        meta = build_metadata(make_script(), config=config, target=target())
        assert PROVENANCE.canonical_url in meta.description

    def test_a_reddit_author_is_credited(self, config: AppConfig) -> None:
        meta = build_metadata(make_script(), config=config, target=target())
        assert "u/throwaway99" in meta.description

    def test_anonymous_platforms_are_not_given_a_fake_author(self, config: AppConfig) -> None:
        """There is no author to credit on a board; claiming one would mislead."""
        anonymous = Provenance(
            source_platform="fourchan",
            source_id="1",
            canonical_url="https://boards.4chan.org/x/thread/1",
            author=None,
            title="thread",
        )
        meta = build_metadata(
            make_script(provenance=anonymous, metadata={"community": "x"}),
            config=config,
            target=target(),
        )
        assert "u/" not in meta.description
        assert "anonymously" in meta.description


class TestTruncation:
    def test_attribution_survives_a_tight_limit(self) -> None:
        """The link is the part that matters; the body is what gets trimmed."""
        meta = VideoMetadata(
            title="t",
            description=("filler " * 200) + "\n\nSource: https://example.com/post",
            tags=(),
            privacy="private",
            source_url="https://example.com/post",
            provenance=PROVENANCE,
        ).truncated(title_max=100, description_max=120)
        assert len(meta.description) <= 120
        assert meta.description.endswith("https://example.com/post")

    def test_short_content_is_untouched(self) -> None:
        meta = VideoMetadata(
            title="short",
            description="also short",
            tags=(),
            privacy="private",
            source_url="u",
            provenance=PROVENANCE,
        ).truncated(title_max=100, description_max=100)
        assert meta.title == "short"
        assert meta.description == "also short"


class TestTags:
    def test_hashes_are_stripped_and_duplicates_removed(self) -> None:
        assert build_tags(["#Shorts", "shorts", "#storytime"]) == ("shorts", "storytime")

    def test_order_is_preserved(self) -> None:
        assert build_tags(["b", "a", "c"]) == ("b", "a", "c")

    def test_the_count_is_capped(self) -> None:
        assert len(build_tags([f"tag{n}" for n in range(50)], limit=5)) == 5

    def test_empty_entries_are_dropped(self) -> None:
        assert build_tags(["", "  ", "#", "real"]) == ("real",)


class TestRegistry:
    def test_the_shipped_publishers_are_registered(self) -> None:
        assert set(available_publishers()) == {"youtube", "instagram", "tiktok"}


class TestRedaction:
    def test_credential_shaped_keys_are_masked(self) -> None:
        cleaned = redacted({"title": "ok", "access_token": "secret", "refresh_token": "secret"})
        assert cleaned["title"] == "ok"
        assert cleaned["access_token"] == "<redacted>"
        assert cleaned["refresh_token"] == "<redacted>"


class TestPublishStates:
    def test_only_published_counts_as_success(self) -> None:
        assert PublishState.PUBLISHED.value == "PUBLISHED"
        assert {state.value for state in PublishState} == {
            "PENDING",
            "UPLOADING",
            "PUBLISHED",
            "FAILED",
            "SKIPPED",
        }
