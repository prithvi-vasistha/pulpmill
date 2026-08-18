"""Word timing derivation.

The claim being tested is narrow and worth stating: sentence boundaries are
*measured* from real clip lengths, and only the distribution of words inside a
sentence is estimated. These tests pin both halves.
"""

from __future__ import annotations

from itertools import pairwise

import pytest

from pulpmill.tts.alignment import concatenate_timings, distribute_words, word_weight


class TestWordDistribution:
    def test_words_fill_the_whole_span(self) -> None:
        timings = distribute_words("one two three four", start=0.0, end=4.0)
        assert timings[0].start_seconds == 0.0
        assert timings[-1].end_seconds == 4.0

    def test_words_are_contiguous(self) -> None:
        timings = distribute_words("a bb ccc dddd", start=1.0, end=3.0)
        for earlier, later in pairwise(timings):
            assert earlier.end_seconds == pytest.approx(later.start_seconds)

    def test_longer_words_get_more_time(self) -> None:
        timings = distribute_words("a extraordinarily", start=0.0, end=2.0)
        assert timings[1].duration_seconds > timings[0].duration_seconds

    def test_punctuation_buys_a_pause(self) -> None:
        """A word followed by a full stop is followed by a real pause."""
        assert word_weight("word.") > word_weight("word")
        assert word_weight("word.") > word_weight("word,")

    def test_a_one_letter_word_still_takes_time(self) -> None:
        assert word_weight("I") >= 1.5

    def test_the_last_word_absorbs_rounding_drift(self) -> None:
        """The final timing must land exactly on the measured clip end."""
        timings = distribute_words(" ".join("word" for _ in range(37)), start=0.0, end=7.0)
        assert timings[-1].end_seconds == 7.0

    def test_empty_text_produces_no_timings(self) -> None:
        assert distribute_words("", start=0.0, end=1.0) == ()

    def test_a_non_positive_span_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="span"):
            distribute_words("some words", start=2.0, end=1.0)

    def test_distribution_is_deterministic(self) -> None:
        first = distribute_words("the same words every time", start=0.0, end=3.0)
        second = distribute_words("the same words every time", start=0.0, end=3.0)
        assert first == second


class TestConcatenation:
    def test_segments_are_laid_out_in_order(self) -> None:
        timings, total = concatenate_timings([("first clip", 1.0), ("second clip", 2.0)])
        assert total == pytest.approx(3.0)
        assert timings[0].word == "first"
        assert timings[-1].word == "clip"
        assert timings[-1].end_seconds == pytest.approx(3.0)

    def test_gaps_push_later_segments_along(self) -> None:
        """The gap is silence: no word may be scheduled inside it."""
        timings, total = concatenate_timings([("first", 1.0), ("second", 1.0)], gaps=[0.5, 0.0])
        assert total == pytest.approx(2.5)
        assert timings[1].start_seconds == pytest.approx(1.5)

    def test_gaps_must_be_parallel_to_segments(self) -> None:
        with pytest.raises(ValueError, match="parallel"):
            concatenate_timings([("a", 1.0)], gaps=[0.1, 0.2])

    def test_a_zero_length_clip_contributes_no_words(self) -> None:
        timings, total = concatenate_timings([("skipped", 0.0), ("kept", 1.0)])
        assert [timing.word for timing in timings] == ["kept"]
        assert total == pytest.approx(1.0)
