"""Multi-part series planning and the TTS provider interface."""

from __future__ import annotations

from itertools import pairwise
from pathlib import Path

import pytest

from pulpmill.domain.series import build_series_id, plan_parts
from pulpmill.domain.story import Provenance
from pulpmill.tts import MockTTSProvider, SpeechRequest, estimate_duration_seconds

PROVENANCE = Provenance(
    source_platform="reddit",
    source_id="t3_abc",
    canonical_url="https://www.reddit.com/r/nosleep/comments/abc/title/",
    author="someone",
    title="A story",
)


class TestSeriesPlanning:
    def test_part_numbering_is_computed_not_invented(self) -> None:
        """The pipeline owns part numbers; no model gets to choose them."""
        _, parts = plan_parts(
            story_id="story-1", provenance=PROVENANCE, boundaries=[100, 200], content_length=300
        )
        assert [part.part_number for part in parts] == [1, 2, 3]
        assert {part.total_parts for part in parts} == {3}
        assert [part.label for part in parts] == ["Part 1/3", "Part 2/3", "Part 3/3"]

    def test_parts_tile_the_content_without_gaps_or_overlap(self) -> None:
        _, parts = plan_parts(
            story_id="story-1", provenance=PROVENANCE, boundaries=[50, 120], content_length=200
        )
        assert parts[0].content_start == 0
        assert parts[-1].content_end == 200
        for earlier, later in pairwise(parts):
            assert earlier.content_end == later.content_start

    def test_planning_is_deterministic(self) -> None:
        first = plan_parts(story_id="s", provenance=PROVENANCE, boundaries=[10], content_length=20)
        second = plan_parts(story_id="s", provenance=PROVENANCE, boundaries=[10], content_length=20)
        assert first[0] == second[0]
        assert [p.id for p in first[1]] == [p.id for p in second[1]]

    def test_boundaries_are_sorted_and_deduplicated(self) -> None:
        _, parts = plan_parts(
            story_id="s", provenance=PROVENANCE, boundaries=[200, 100, 100], content_length=300
        )
        assert len(parts) == 3

    def test_out_of_range_boundaries_are_ignored(self) -> None:
        _, parts = plan_parts(
            story_id="s", provenance=PROVENANCE, boundaries=[0, 500, -20], content_length=300
        )
        assert len(parts) == 1

    def test_a_story_with_no_cuts_is_a_single_part(self) -> None:
        _, parts = plan_parts(
            story_id="s", provenance=PROVENANCE, boundaries=[], content_length=300
        )
        assert len(parts) == 1
        assert parts[0].label == "Part 1/1"

    def test_every_part_keeps_the_source_url(self) -> None:
        """Provenance survives the split -- video -> part -> story -> source."""
        _, parts = plan_parts(
            story_id="s", provenance=PROVENANCE, boundaries=[100], content_length=200
        )
        for part in parts:
            assert part.provenance.canonical_url == PROVENANCE.canonical_url
            assert part.provenance.source_id == "t3_abc"

    def test_part_text_slices_the_original_content(self) -> None:
        content = "".join(str(index % 10) for index in range(100))
        _, parts = plan_parts(
            story_id="s", provenance=PROVENANCE, boundaries=[40], content_length=len(content)
        )
        assert parts[0].text(content) + parts[1].text(content) == content

    def test_series_ids_are_stable_and_revision_aware(self) -> None:
        assert build_series_id("s") == build_series_id("s")
        assert build_series_id("s", 1) != build_series_id("s", 2)

    def test_empty_content_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="content_length"):
            plan_parts(story_id="s", provenance=PROVENANCE, boundaries=[], content_length=0)


class TestTTSProvider:
    def test_the_mock_provider_is_always_available(self) -> None:
        available, detail = MockTTSProvider().available()
        assert available is True
        assert detail

    def test_synthesis_produces_a_real_file_with_timings(self, tmp_path: Path) -> None:
        provider = MockTTSProvider()
        request = SpeechRequest(text="one two three four five", voice_id="mock-neutral")
        result = provider.synthesize(request, output_dir=tmp_path)

        assert result.audio_path.exists()
        assert result.audio_path.stat().st_size > 0
        assert result.duration_seconds > 0
        assert result.voice_id == "mock-neutral"
        assert result.model_version == provider.model_version
        assert len(result.word_timings) == 5
        assert result.word_timings[0].start_seconds == 0.0

    def test_word_timings_are_ordered_and_contiguous(self, tmp_path: Path) -> None:
        result = MockTTSProvider().synthesize(
            SpeechRequest(text="alpha beta gamma", voice_id="mock-warm"), output_dir=tmp_path
        )
        for earlier, later in pairwise(result.word_timings):
            assert earlier.end_seconds == pytest.approx(later.start_seconds)

    def test_identical_requests_reuse_the_cached_audio(self, tmp_path: Path) -> None:
        """Regenerating 200 videos a week makes this difference material."""
        provider = MockTTSProvider()
        request = SpeechRequest(text="cache me", voice_id="mock-neutral")
        first = provider.synthesize(request, output_dir=tmp_path)
        second = provider.synthesize(request, output_dir=tmp_path)
        assert first.cached is False
        assert second.cached is True
        assert first.audio_path == second.audio_path

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            (
                SpeechRequest(text="a", voice_id="v1"),
                SpeechRequest(text="b", voice_id="v1"),
            ),
            (
                SpeechRequest(text="a", voice_id="v1"),
                SpeechRequest(text="a", voice_id="v2"),
            ),
            (
                SpeechRequest(text="a", voice_id="v1", speed=1.0),
                SpeechRequest(text="a", voice_id="v1", speed=1.2),
            ),
        ],
    )
    def test_anything_affecting_the_audio_changes_the_cache_key(
        self, left: SpeechRequest, right: SpeechRequest
    ) -> None:
        assert left.cache_key(model_version="m") != right.cache_key(model_version="m")

    def test_the_model_version_is_part_of_the_cache_key(self) -> None:
        request = SpeechRequest(text="a", voice_id="v1")
        assert request.cache_key(model_version="m1") != request.cache_key(model_version="m2")

    def test_duration_estimation_scales_with_length(self) -> None:
        assert estimate_duration_seconds(0) == 0.0
        assert estimate_duration_seconds(150) == pytest.approx(60.0)
        assert estimate_duration_seconds(300) == pytest.approx(120.0)
