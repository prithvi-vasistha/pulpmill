"""Speech shaping: prose in, narratable text out.

These cases are drawn from the communities the pipeline actually ingests. Each
one exists because a synthesiser reads the raw form wrong.
"""

from __future__ import annotations

import pytest

from pulpmill.scripting.speech import (
    spell_decimal,
    spell_integer,
    spell_ordinal,
    spell_year,
    to_speech_text,
)


class TestNumberSpelling:
    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (0, "zero"),
            (7, "seven"),
            (13, "thirteen"),
            (20, "twenty"),
            (42, "forty two"),
            (100, "one hundred"),
            (118, "one hundred eighteen"),
            (1000, "one thousand"),
            (1250, "one thousand two hundred fifty"),
            (1_000_000, "one million"),
        ],
    )
    def test_integers(self, value: int, expected: str) -> None:
        assert spell_integer(value) == expected

    def test_negatives_are_spoken(self) -> None:
        assert spell_integer(-5) == "negative five"

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1, "first"),
            (2, "second"),
            (3, "third"),
            (5, "fifth"),
            (21, "twenty first"),
            (30, "thirtieth"),
        ],
    )
    def test_ordinals(self, value: int, expected: str) -> None:
        assert spell_ordinal(value) == expected

    @pytest.mark.parametrize(
        ("value", "expected"),
        [
            (1998, "nineteen ninety eight"),
            (2024, "twenty twenty four"),
            (2005, "twenty oh five"),
            (1900, "nineteen hundred"),
        ],
    )
    def test_years_read_as_pairs(self, value: int, expected: str) -> None:
        """A year is not a cardinal number. 1998 is not "one thousand ...”."""
        assert spell_year(value) == expected

    def test_years_outside_the_pair_range_stay_cardinal(self) -> None:
        assert spell_year(500) == "five hundred"

    def test_decimals(self) -> None:
        assert spell_decimal("3.5") == "three point five"
        assert spell_decimal("1,250") == "one thousand two hundred fifty"


class TestPlatformShorthand:
    def test_aita_is_expanded_not_spelled(self) -> None:
        """ "ay eye tee ay" instantly sounds like a machine."""
        assert to_speech_text("AITA for leaving?").startswith("Am I the asshole")

    def test_relationship_abbreviations(self) -> None:
        spoken = to_speech_text("My SIL told my MIL that my DH lied.")
        assert "sister in law" in spoken
        assert "mother in law" in spoken
        assert "dear husband" in spoken

    def test_lowercase_ambiguous_words_are_left_alone(self) -> None:
        """ "so" and "ex" are ordinary English; "SO" and "EX" are not.

        Expanding these case-insensitively would rewrite half the corpus into
        nonsense, which is why the case rule exists.
        """
        spoken = to_speech_text("I was so tired, so I left.")
        assert "significant other" not in spoken

    def test_uppercase_forms_are_expanded(self) -> None:
        assert "significant other" in to_speech_text("My SO disagreed.")

    def test_unknown_acronyms_are_read_as_letters(self) -> None:
        assert "N D A" in to_speech_text("I signed an NDA.")


class TestCompoundPatterns:
    def test_age_and_gender_markers(self) -> None:
        """(28F) is extremely common in these communities and reads terribly."""
        assert "twenty eight female" in to_speech_text("My sister (28F) called.")
        assert "thirty two male" in to_speech_text("I (32M) called.")

    def test_money(self) -> None:
        assert to_speech_text("It cost $40.") == "It cost forty dollars."
        assert to_speech_text("It cost $1.") == "It cost one dollar."

    def test_currency_decimals_are_read_as_money(self) -> None:
        assert "four dollars and fifty cents" in to_speech_text("It was $4.50 each.")

    def test_magnitude_suffixes_are_multiplied_out(self) -> None:
        assert "two thousand five hundred dollars" in to_speech_text("I made $2.5k.")

    def test_money_does_not_lose_the_following_space(self) -> None:
        """Regression: "$1 and" produced "one dollarand"."""
        assert "one dollar and took" in to_speech_text("It cost $1 and took time.")

    def test_a_money_magnitude_is_not_mistaken_for_a_gender_marker(self) -> None:
        """Regression: "$3M" was read as a thirty-something male."""
        assert "three million dollars" in to_speech_text("a $3M house")

    def test_times(self) -> None:
        assert "three P M" in to_speech_text("He arrived at 3pm.")

    def test_heights(self) -> None:
        assert "six foot two" in to_speech_text("He is 6'2\" tall.")

    def test_percentages(self) -> None:
        assert "forty percent" in to_speech_text("She was 40% sure.")


class TestPunctuationAndMarkup:
    def test_emphatic_punctuation_collapses(self) -> None:
        """Regression: a backreference caught "!!!" but not "?!?!"."""
        assert to_speech_text("Really?!?!") == "Really?"
        assert to_speech_text("No!!!") == "No!"

    def test_ellipses_become_a_pause(self) -> None:
        assert to_speech_text("I waited... then left.") == "I waited, then left."

    def test_greentext_markers_are_stripped(self) -> None:
        """ ">be me" is meaningful on a board and meaningless read aloud."""
        assert to_speech_text(">be me\n>at work") == "be me at work"

    def test_symbols_are_spoken(self) -> None:
        assert "and" in to_speech_text("Tom & Jerry")

    def test_empty_input_is_empty_output(self) -> None:
        assert to_speech_text("") == ""
        assert to_speech_text("   ") == ""

    def test_punctuation_only_input_produces_nothing_speakable(self) -> None:
        """The script stage drops these lines rather than synthesising them."""
        assert to_speech_text("...").strip(" ,") == ""


class TestIdempotenceOfShape:
    def test_ordinary_prose_is_left_intact(self) -> None:
        text = "I told her the truth and she stopped speaking to me."
        assert to_speech_text(text) == text
