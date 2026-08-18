"""Caption cue grouping and ASS generation."""

from __future__ import annotations

from itertools import pairwise

import pytest

from pulpmill.captions.ass import build_ass, escape_text, format_timestamp
from pulpmill.captions.cues import group_into_cues
from pulpmill.config.models import CaptionConfig, TitleCardConfig
from pulpmill.domain.media import WordTiming
from pulpmill.tts.alignment import distribute_words


def timings(text: str, start: float, end: float) -> list[WordTiming]:
    return list(distribute_words(text, start=start, end=end))


class TestCueGrouping:
    def test_words_are_grouped_within_the_budget(self) -> None:
        config = CaptionConfig(max_words_per_cue=3, max_chars_per_cue=40)
        cues = group_into_cues(timings("one two three four five six", 0.0, 6.0), config)
        assert all(len(cue.words) <= 3 for cue in cues)

    def test_a_pause_forces_a_break(self) -> None:
        """A caption spanning a breath reads as broken sync."""
        first = timings("She said nothing", 0.0, 1.5)
        second = [t.shifted(2.5) for t in timings("Then she left", 0.0, 1.2)]
        cues = group_into_cues(first + second, CaptionConfig())
        assert any(cue.text.endswith("nothing") for cue in cues)
        assert any(cue.text.startswith("Then") for cue in cues)

    def test_terminal_punctuation_forces_a_break(self) -> None:
        cues = group_into_cues(timings("It ended. A new thing began", 0.0, 4.0), CaptionConfig())
        assert cues[0].text.endswith(".")

    def test_cues_never_overlap_and_stay_ordered(self) -> None:
        cues = group_into_cues(timings("a b c d e f g h i j k l", 0.0, 8.0), CaptionConfig())
        for earlier, later in pairwise(cues):
            assert earlier.end_seconds <= later.start_seconds
            assert earlier.index < later.index

    def test_every_word_appears_exactly_once(self) -> None:
        text = "one two three four five six seven eight nine ten"
        cues = group_into_cues(timings(text, 0.0, 5.0), CaptionConfig())
        assert " ".join(cue.text for cue in cues) == text

    def test_a_flashed_cue_is_merged_into_its_neighbour(self) -> None:
        """A caption on screen for 120ms is noise, not information."""
        config = CaptionConfig(min_cue_seconds=1.0, max_words_per_cue=2)
        cues = group_into_cues(timings("a b c d", 0.0, 0.6), config)
        assert len(cues) == 1

    def test_no_timings_means_no_cues(self) -> None:
        assert group_into_cues([], CaptionConfig()) == ()


class TestTimestamps:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0.0, "0:00:00.00"), (1.5, "0:00:01.50"), (61.25, "0:01:01.25"), (3725.456, "1:02:05.46")],
    )
    def test_formatting(self, seconds: float, expected: str) -> None:
        assert format_timestamp(seconds) == expected

    def test_negative_times_are_clamped(self) -> None:
        assert format_timestamp(-3.0) == "0:00:00.00"


class TestAssDocument:
    def test_document_has_the_required_sections(self) -> None:
        cues = group_into_cues(timings("hello there friend", 0.0, 2.0), CaptionConfig())
        doc = build_ass(cues, CaptionConfig(), width=1080, height=1920)
        assert "[Script Info]" in doc
        assert "[V4+ Styles]" in doc
        assert "[Events]" in doc
        assert "PlayResX: 1080" in doc
        assert "PlayResY: 1920" in doc

    def test_karaoke_emits_one_event_per_word(self) -> None:
        """ASS's own \\k colours every already-spoken word, which is the wrong
        look. One event per word highlights only the word being said."""
        config = CaptionConfig(karaoke=True, max_words_per_cue=3)
        cues = group_into_cues(timings("one two three", 0.0, 3.0), config)
        doc = build_ass(cues, config, width=1080, height=1920)
        dialogue = [line for line in doc.splitlines() if line.startswith("Dialogue: 0")]
        assert len(dialogue) == 3
        assert all(config.highlight_colour in line for line in dialogue)

    def test_karaoke_can_be_turned_off(self) -> None:
        config = CaptionConfig(karaoke=False, max_words_per_cue=3)
        cues = group_into_cues(timings("one two three", 0.0, 3.0), config)
        doc = build_ass(cues, config, width=1080, height=1920)
        assert len([line for line in doc.splitlines() if line.startswith("Dialogue")]) == 1

    def test_the_title_card_is_a_separate_style_and_layer(self) -> None:
        cues = group_into_cues(timings("body text here", 0.0, 2.0), CaptionConfig())
        doc = build_ass(
            cues,
            CaptionConfig(),
            width=1080,
            height=1920,
            title_card=TitleCardConfig(),
            title_text="A Title",
        )
        assert "Style: Title," in doc
        assert any(line.startswith("Dialogue: 1") for line in doc.splitlines())

    def test_wrapping_is_enabled_so_long_cues_do_not_clip(self) -> None:
        """Regression: WrapStyle 2 clipped long text off both edges of frame."""
        doc = build_ass((), CaptionConfig(), width=1080, height=1920)
        assert "WrapStyle: 0" in doc

    def test_vertical_position_is_measured_from_the_top(self) -> None:
        doc = build_ass((), CaptionConfig(vertical_position=0.75), width=1080, height=1920)
        style = next(line for line in doc.splitlines() if line.startswith("Style: Caption"))
        # Alignment 2 measures MarginV up from the bottom edge.
        assert style.rsplit(",", 2)[-2] == str(round(0.25 * 1920))


class TestUntrustedText:
    """Scraped text is data, never ASS override syntax."""

    def test_override_tags_in_a_story_cannot_reposition_captions(self) -> None:
        assert "{" not in escape_text("{\\an8}gotcha")
        assert "\\" not in escape_text("{\\an8}gotcha")

    def test_newlines_are_flattened(self) -> None:
        assert "\n" not in escape_text("line one\nline two")

    def test_injection_through_a_cue_does_not_reach_the_document(self) -> None:
        cues = group_into_cues(
            timings("{\\an8}hello {\\c&HFF0000&}there", 0.0, 2.0), CaptionConfig()
        )
        doc = build_ass(cues, CaptionConfig(karaoke=False), width=1080, height=1920)
        events = [line for line in doc.splitlines() if line.startswith("Dialogue")]
        assert events
        for line in events:
            payload = line.split(",,", 1)[1]
            # `{` and `\\` are what make an override an override. Once those are
            # gone the rest is inert text that libass draws as-is, which is the
            # correct outcome -- the story said it, so the caption shows it.
            assert "{" not in payload
            assert "}" not in payload
            assert "\\" not in payload
