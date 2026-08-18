"""The narration script model, hooks and titles."""

from __future__ import annotations

from datetime import UTC, datetime
from itertools import pairwise

import pytest

from pulpmill.domain.script import (
    LineRole,
    NarrationScript,
    ScriptLine,
    build_script_id,
)
from pulpmill.domain.series import plan_parts
from pulpmill.domain.story import Provenance
from pulpmill.scripting.hooks import build_hook, build_outro, tidy_title
from pulpmill.scripting.provider import validate_cut_points

PROVENANCE = Provenance(
    source_platform="reddit",
    source_id="abc123",
    canonical_url="https://www.reddit.com/r/x/comments/abc123/title/",
    author="someone",
    title="A title",
)


def line(index: int, role: LineRole = LineRole.BODY, text: str = "Some text.") -> ScriptLine:
    return ScriptLine(index=index, role=role, text=text, speech_text=text)


def script(**overrides: object) -> NarrationScript:
    defaults: dict[str, object] = {
        "id": build_script_id("story-1", 1),
        "story_id": "story-1",
        "part_number": 1,
        "total_parts": 1,
        "series_id": None,
        "part_id": None,
        "provenance": PROVENANCE,
        "title": "A title",
        "lines": (line(0, LineRole.HOOK, "Hook line."), line(1)),
        "generator": "deterministic",
        "generator_version": "test",
        "config_fingerprint": "abc",
        "created_at": datetime(2026, 1, 1, tzinfo=UTC),
    }
    defaults.update(overrides)
    return NarrationScript(**defaults)  # type: ignore[arg-type]


class TestScriptIds:
    def test_ids_are_deterministic(self) -> None:
        assert build_script_id("story-1", 2) == build_script_id("story-1", 2)

    def test_parts_get_distinct_ids(self) -> None:
        assert build_script_id("story-1", 1) != build_script_id("story-1", 2)

    def test_part_numbers_are_one_indexed(self) -> None:
        with pytest.raises(ValueError, match="1-indexed"):
            build_script_id("story-1", 0)


class TestScriptInvariants:
    def test_a_script_needs_a_body(self) -> None:
        """A hook and an outro with nothing between them is not a video."""
        with pytest.raises(ValueError, match="body line"):
            script(lines=(line(0, LineRole.HOOK), line(1, LineRole.OUTRO)))

    def test_a_script_needs_lines(self) -> None:
        with pytest.raises(ValueError, match="at least one line"):
            script(lines=())

    def test_line_indices_must_be_contiguous(self) -> None:
        with pytest.raises(ValueError, match="indexed"):
            script(lines=(line(0), line(2)))

    def test_part_number_must_fit_the_total(self) -> None:
        with pytest.raises(ValueError, match="out of range"):
            script(part_number=3, total_parts=2)

    def test_a_line_must_have_speakable_text(self) -> None:
        with pytest.raises(ValueError, match="no speakable text"):
            ScriptLine(index=0, role=LineRole.BODY, text="...", speech_text="   ")

    def test_provenance_travels_with_the_script(self) -> None:
        """A rendered file must know the URL it came from."""
        assert script().provenance.canonical_url == PROVENANCE.canonical_url


class TestScriptProperties:
    def test_a_single_part_has_no_label(self) -> None:
        assert script().label == ""
        assert script().is_series is False

    def test_a_series_part_is_labelled(self) -> None:
        assert script(part_number=2, total_parts=4).label == "Part 2/4"

    def test_speech_text_joins_every_line(self) -> None:
        assert "Hook line." in script().speech_text
        assert "Some text." in script().speech_text

    def test_duration_estimate_scales_with_rate(self) -> None:
        fast = script().estimated_seconds(words_per_minute=300.0)
        slow = script().estimated_seconds(words_per_minute=150.0)
        assert slow == pytest.approx(fast * 2)

    def test_a_zero_rate_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            script().estimated_seconds(words_per_minute=0)


class TestTitles:
    def test_platform_furniture_is_stripped(self) -> None:
        assert tidy_title("[UPDATE] AITA for leaving? (long, sorry)") == "AITA for leaving?"

    def test_shouting_is_normalised(self) -> None:
        assert tidy_title("MY LANDLORD KEEPS ENTERING") == "My landlord keeps entering"

    def test_an_existing_part_marker_is_removed(self) -> None:
        """The pipeline assigns part numbers; a source's own claim is ignored."""
        assert tidy_title("Something happened - part 2 of 3") == "Something happened"

    def test_long_titles_are_cut_on_a_word_boundary(self) -> None:
        result = tidy_title("word " * 60, max_chars=40)
        assert len(result) <= 40
        assert result.endswith("...")

    def test_stripping_never_returns_nothing(self) -> None:
        """A wrong title card beats an empty one."""
        assert tidy_title("[UPDATE]") != ""

    def test_empty_input_stays_empty(self) -> None:
        assert tidy_title("") == ""


class TestHooks:
    def test_the_title_is_the_hook_for_part_one(self) -> None:
        """These communities write their titles to be clicked already."""
        assert (
            build_hook(
                title="AITA for saying no?", first_sentence="x", part_number=1, total_parts=1
            )
            == "AITA for saying no?"
        )

    def test_a_missing_terminator_is_added(self) -> None:
        assert (
            build_hook(title="It happened again", first_sentence="x", part_number=1, total_parts=1)
            == "It happened again."
        )

    def test_later_parts_announce_themselves(self) -> None:
        hook = build_hook(
            title="AITA for saying no?", first_sentence="x", part_number=2, total_parts=3
        )
        assert hook == "AITA for saying no? Part 2."

    def test_a_titleless_source_opens_on_the_story(self) -> None:
        """4chan threads frequently have no subject at all."""
        assert (
            build_hook(
                title="", first_sentence="It started on a Tuesday.", part_number=1, total_parts=1
            )
            == "It started on a Tuesday."
        )


class TestOutros:
    def test_a_middle_part_points_at_the_next(self) -> None:
        assert (
            build_outro(
                part_number=1,
                total_parts=3,
                template="Part {next_part} is up next.",
                final_outro="x",
            )
            == "Part 2 is up next."
        )

    def test_the_final_part_uses_the_final_outro(self) -> None:
        assert (
            build_outro(
                part_number=3, total_parts=3, template="Part {next_part}.", final_outro="Follow."
            )
            == "Follow."
        )

    def test_an_empty_template_means_no_outro(self) -> None:
        assert build_outro(part_number=3, total_parts=3, template="x", final_outro="") is None


class TestCutPointValidation:
    """A model may advise on pacing; it may not invent structure."""

    def test_valid_cuts_pass_through(self) -> None:
        assert validate_cut_points([3, 7], sentence_count=20, max_parts=6) == (3, 7)

    def test_no_cuts_is_valid(self) -> None:
        assert validate_cut_points([], sentence_count=20, max_parts=6) == ()

    def test_unsorted_cuts_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="ascending"):
            validate_cut_points([7, 3], sentence_count=20, max_parts=6)

    def test_repeated_cuts_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="ascending"):
            validate_cut_points([3, 3], sentence_count=20, max_parts=6)

    def test_out_of_range_cuts_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="inside"):
            validate_cut_points([900], sentence_count=20, max_parts=6)

    def test_a_cut_at_the_final_sentence_is_rejected(self) -> None:
        """It would produce an empty final part."""
        with pytest.raises(ValueError, match="inside"):
            validate_cut_points([19], sentence_count=20, max_parts=6)

    def test_too_many_parts_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="maximum"):
            validate_cut_points([1, 2, 3, 4], sentence_count=20, max_parts=3)


class TestPartNumbering:
    def test_numbering_is_computed_from_boundaries(self) -> None:
        """The pipeline assigns part_number and total_parts, never a model."""
        _, parts = plan_parts(
            story_id="story-1", provenance=PROVENANCE, boundaries=[100, 200], content_length=300
        )
        assert [part.part_number for part in parts] == [1, 2, 3]
        assert {part.total_parts for part in parts} == {3}

    def test_parts_cover_the_whole_story(self) -> None:
        _, parts = plan_parts(
            story_id="story-1", provenance=PROVENANCE, boundaries=[100, 200], content_length=300
        )
        assert parts[0].content_start == 0
        assert parts[-1].content_end == 300
        for earlier, later in pairwise(parts):
            assert earlier.content_end == later.content_start

    def test_planning_is_idempotent(self) -> None:
        first = plan_parts(story_id="s", provenance=PROVENANCE, boundaries=[50], content_length=100)
        second = plan_parts(
            story_id="s", provenance=PROVENANCE, boundaries=[50], content_length=100
        )
        assert first[0] == second[0]
        assert [p.id for p in first[1]] == [p.id for p in second[1]]

    def test_parts_carry_provenance(self) -> None:
        _, parts = plan_parts(
            story_id="s", provenance=PROVENANCE, boundaries=[50], content_length=100
        )
        assert all(part.provenance.canonical_url == PROVENANCE.canonical_url for part in parts)
