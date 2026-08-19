"""Sentence splitting and part planning."""

from __future__ import annotations

import pytest

from pulpmill.domain.errors import StoryTooLongError
from pulpmill.scripting.segmentation import (
    plan_segments,
    speech_durations,
    split_sentences,
    subdivide_long_sentences,
)
from pulpmill.scripting.speech import to_speech_text


def durations_for(sentences: list, *, wpm: float = 150.0) -> list[float]:
    return speech_durations([sentence.text for sentence in sentences], words_per_minute=wpm)


class TestSentenceSplitting:
    def test_basic_sentences(self) -> None:
        result = split_sentences("One thing happened. Then another. And a third!")
        assert [s.text for s in result] == [
            "One thing happened.",
            "Then another.",
            "And a third!",
        ]

    def test_abbreviations_do_not_end_a_sentence(self) -> None:
        result = split_sentences("Dr. Alvarez arrived. Mr. Lee did not.")
        assert len(result) == 2

    def test_decimals_do_not_end_a_sentence(self) -> None:
        assert len(split_sentences("It rose 3.5 percent overnight.")) == 1

    def test_a_sentence_really_can_end_in_no(self) -> None:
        """Regression: "no" was treated as an abbreviation, merging sentences.

        "She said no." is one of the most common sentences in this corpus, and
        merging it with the next one is far worse than an occasional bad split.
        """
        assert len(split_sentences("She said no. Then she left.")) == 2

    def test_paragraph_breaks_are_recorded(self) -> None:
        result = split_sentences("First para.\n\nSecond para. More here.")
        assert [s.paragraph_break for s in result] == [False, True, False]

    def test_offsets_point_back_into_the_source(self) -> None:
        """Provenance: a part must resolve to the exact text it was cut from."""
        text = "First one here. Second one here.\n\nThird one here."
        for sentence in split_sentences(text):
            assert text[sentence.start : sentence.end].strip() == sentence.text

    def test_repeated_text_does_not_confuse_offsets(self) -> None:
        """Regression: a search-based implementation mislocated repeats."""
        text = "Same line. Same line. Same line."
        result = split_sentences(text)
        assert [s.start for s in result] == sorted({s.start for s in result})
        assert len(result) == 3

    def test_empty_input(self) -> None:
        assert split_sentences("") == []
        assert split_sentences("   \n\n  ") == []


class TestPartPlanning:
    def test_a_short_story_is_one_part(self) -> None:
        sentences = split_sentences("A short story. It ends quickly.")
        plan = plan_segments(
            sentences,
            durations=durations_for(sentences),
            target_seconds=55.0,
            max_seconds=170.0,
            max_parts=6,
        )
        assert plan.total_parts == 1
        assert plan.boundaries == ()

    def test_a_long_story_is_split_towards_the_target(self) -> None:
        sentences = split_sentences(" ".join(f"Sentence number {n} here." for n in range(1, 200)))
        plan = plan_segments(
            sentences,
            durations=durations_for(sentences),
            target_seconds=20.0,
            max_seconds=60.0,
            max_parts=8,
        )
        assert plan.total_parts > 1
        assert max(plan.estimated_seconds) <= 60.0

    def test_parts_are_balanced_rather_than_greedily_filled(self) -> None:
        """A greedy fill reliably leaves a stub final part."""
        sentences = split_sentences(" ".join(f"Sentence {n} of the story." for n in range(1, 120)))
        plan = plan_segments(
            sentences,
            durations=durations_for(sentences),
            target_seconds=20.0,
            max_seconds=60.0,
            max_parts=8,
        )
        shortest, longest = min(plan.estimated_seconds), max(plan.estimated_seconds)
        assert longest - shortest < longest * 0.4

    def test_every_sentence_lands_in_exactly_one_part(self) -> None:
        """No content is dropped, and none is duplicated across parts."""
        sentences = split_sentences(" ".join(f"Line {n} here." for n in range(1, 90)))
        plan = plan_segments(
            sentences,
            durations=durations_for(sentences),
            target_seconds=10.0,
            max_seconds=40.0,
            max_parts=8,
        )
        covered: list[int] = []
        for start, stop in plan.ranges:
            covered.extend(range(start, stop))
        assert covered == list(range(len(sentences)))

    def test_a_story_that_cannot_fit_is_rejected_not_truncated(self) -> None:
        """Publishing six parts of a seventeen-part story strands the viewer.

        Rejecting it is the honest outcome: the story is not publishable in
        this format, and half of it is worse than none of it.
        """
        sentences = split_sentences(" ".join(f"Sentence {n} of many." for n in range(1, 600)))
        with pytest.raises(StoryTooLongError):
            plan_segments(
                sentences,
                durations=durations_for(sentences),
                target_seconds=20.0,
                max_seconds=40.0,
                max_parts=3,
            )

    def test_parts_stretch_towards_the_maximum_before_giving_up(self) -> None:
        """A longer part that finishes the story beats one that abandons it."""
        sentences = split_sentences(" ".join(f"Sentence {n} of many." for n in range(1, 100)))
        durations = durations_for(sentences)
        plan = plan_segments(
            sentences,
            durations=durations,
            target_seconds=10.0,
            max_seconds=120.0,
            max_parts=3,
        )
        assert plan.total_parts <= 3
        assert sum(plan.estimated_seconds) == pytest.approx(sum(durations))

    def test_durations_must_be_parallel_to_sentences(self) -> None:
        sentences = split_sentences("One. Two. Three.")
        with pytest.raises(ValueError, match="parallel"):
            plan_segments(
                sentences,
                durations=[1.0],
                target_seconds=10.0,
                max_seconds=20.0,
                max_parts=4,
            )

    def test_planning_is_deterministic(self) -> None:
        sentences = split_sentences(" ".join(f"Sentence {n} here." for n in range(1, 80)))
        durations = durations_for(sentences)
        kwargs = {
            "durations": durations,
            "target_seconds": 15.0,
            "max_seconds": 45.0,
            "max_parts": 8,
        }
        assert plan_segments(sentences, **kwargs) == plan_segments(sentences, **kwargs)


class TestSpeechDurations:
    def test_durations_measure_the_spoken_form(self) -> None:
        """Regression: planning against the written form underestimated badly.

        Speech shaping expands "AITA" into four spoken words. Planning on the
        page produces parts that reliably overrun.
        """
        written = speech_durations(["AITA for leaving"], words_per_minute=150.0)
        spoken = speech_durations(["Am I the asshole for leaving"], words_per_minute=150.0)
        assert spoken[0] > written[0]

    def test_zero_wpm_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            speech_durations(["anything"], words_per_minute=0)


class TestSubdividingRunOns:
    """A run-on post is a normal input on these platforms, not an edge case.

    Two hard limits make subdivision necessary: the synthesiser refuses inputs
    past ~85 spoken words, and a sentence is the atom part planning works with,
    so an over-long one forces a part past `max_seconds` however the planner
    cuts.
    """

    def run_on(self, repeats: int = 12) -> str:
        return " ".join(
            ["so then she told me that the whole family already knew about it"] * repeats
        )

    def test_a_long_sentence_is_broken_up(self) -> None:
        sentences = split_sentences(self.run_on())
        assert len(sentences) == 1

        pieces = subdivide_long_sentences(sentences, max_words=30)
        assert len(pieces) > 1
        assert all(len(piece.text.split()) <= 30 for piece in pieces)

    def test_a_short_sentence_is_left_alone(self) -> None:
        sentences = split_sentences("This one is already short enough.")
        assert subdivide_long_sentences(sentences, max_words=30) == sentences

    def test_no_words_are_lost(self) -> None:
        sentences = split_sentences(self.run_on())
        pieces = subdivide_long_sentences(sentences, max_words=25)
        assert " ".join(p.text for p in pieces).split() == sentences[0].text.split()

    def test_offsets_stay_ordered_and_inside_the_source(self) -> None:
        """Provenance: a part still resolves to the text it was cut from."""
        text = self.run_on()
        pieces = subdivide_long_sentences(split_sentences(text), max_words=25)
        assert [p.start for p in pieces] == sorted(p.start for p in pieces)
        for piece in pieces:
            assert 0 <= piece.start < piece.end <= len(text)

    def test_only_the_first_piece_keeps_the_paragraph_break(self) -> None:
        """The rest are continuations of the same thought, not new paragraphs."""
        text = "Short opener.\n\n" + self.run_on()
        pieces = subdivide_long_sentences(split_sentences(text), max_words=25)
        breaks = [index for index, piece in enumerate(pieces) if piece.paragraph_break]
        assert len(breaks) == 1

    def test_the_limit_is_measured_on_the_spoken_form(self) -> None:
        """ "AITA" is one word on the page and four in the narration.

        Measuring the page would let a piece through that the model then
        refuses, which is the failure this whole mechanism exists to prevent.
        """
        spoken = lambda text: len(to_speech_text(text).split())  # noqa: E731
        text = "AITA " * 40
        pieces = subdivide_long_sentences(split_sentences(text), max_words=30, measure=spoken)
        assert all(spoken(piece.text) <= 30 for piece in pieces)

    def test_a_clause_with_no_boundaries_still_splits(self) -> None:
        """Falls back to word boundaries when there is nothing better."""
        text = "word " * 200
        pieces = subdivide_long_sentences(split_sentences(text), max_words=40)
        assert all(len(piece.text.split()) <= 40 for piece in pieces)

    def test_a_zero_limit_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            subdivide_long_sentences(split_sentences("Anything here."), max_words=0)

    def test_subdivision_keeps_parts_inside_the_ceiling(self) -> None:
        """The regression this fixes: one 285-word run-on forced a 114s part.

        `max_seconds` cannot be honoured if a single atom exceeds it, so the
        atom has to be broken first.
        """
        sentences = split_sentences(self.run_on(30))
        pieces = subdivide_long_sentences(sentences, max_words=70)
        durations = speech_durations([p.text for p in pieces], words_per_minute=185.0)
        plan = plan_segments(
            pieces, durations=durations, target_seconds=75.0, max_seconds=90.0, max_parts=10
        )
        assert max(plan.estimated_seconds) <= 90.0
