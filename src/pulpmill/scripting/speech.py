"""Turning readable prose into text a speech model reads correctly.

Every transformation here exists because a synthesiser gets the raw form wrong.
The examples are drawn from the communities this pipeline actually ingests:

    "AITA for telling my SIL (28F) to leave?"
        -> "Am I the asshole for telling my sister in law, twenty eight female,
            to leave?"
    "He owed me $1,250 by 3pm on the 21st."
        -> "He owed me one thousand two hundred fifty dollars by three P M on
            the twenty first."

The output is never displayed. Captions render `ScriptLine.text`, the original;
only `speech_text` goes to the synthesiser. Keeping both is what lets a caption
show "$1,250" while the narrator says it properly.

Everything is a pure string transformation over untrusted scraped text: no
evaluation, no shelling out, no network.
"""

from __future__ import annotations

import re

_ONES = (
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
)
_TENS = ("", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety")
_SCALES = ((1_000_000_000, "billion"), (1_000_000, "million"), (1_000, "thousand"))

_ORDINAL_WORDS = {
    "one": "first",
    "two": "second",
    "three": "third",
    "five": "fifth",
    "eight": "eighth",
    "nine": "ninth",
    "twelve": "twelfth",
}


def spell_integer(value: int) -> str:
    """Spell a whole number in words. Handles the full 64-bit range sensibly."""
    if value < 0:
        return f"negative {spell_integer(-value)}"
    if value < 20:
        return _ONES[value]
    if value < 100:
        tens, ones = divmod(value, 10)
        return _TENS[tens] if not ones else f"{_TENS[tens]} {_ONES[ones]}"
    if value < 1000:
        hundreds, rest = divmod(value, 100)
        head = f"{_ONES[hundreds]} hundred"
        return head if not rest else f"{head} {spell_integer(rest)}"
    for scale, name in _SCALES:
        if value >= scale:
            count, rest = divmod(value, scale)
            head = f"{spell_integer(count)} {name}"
            return head if not rest else f"{head} {spell_integer(rest)}"
    # Beyond the named scales, reading digits is better than being wrong.
    return " ".join(_ONES[int(digit)] for digit in str(value))


def spell_ordinal(value: int) -> str:
    """Spell an ordinal: 21 -> "twenty first"."""
    words = spell_integer(value)
    head, _, last = words.rpartition(" ")
    if last in _ORDINAL_WORDS:
        tail = _ORDINAL_WORDS[last]
    elif last.endswith("y"):
        tail = f"{last[:-1]}ieth"
    else:
        tail = f"{last}th"
    return f"{head} {tail}".strip()


def spell_year(value: int) -> str:
    """Read a year the way a person does: 1998 -> "nineteen ninety eight".

    Only applied in the range where pair-reading is actually how English works.
    Outside it -- and for round centuries, where "nineteen zero zero" is wrong --
    the ordinary cardinal reading is correct.
    """
    if not 1100 <= value <= 2099:
        return spell_integer(value)
    high, low = divmod(value, 100)
    if low == 0:
        return f"{spell_integer(high)} hundred"
    if low < 10:
        return f"{spell_integer(high)} oh {_ONES[low]}"
    return f"{spell_integer(high)} {spell_integer(low)}"


def spell_decimal(text: str) -> str:
    """Spell a possibly-decimal, possibly-comma-grouped numeric string."""
    cleaned = text.replace(",", "")
    if "." not in cleaned:
        return spell_integer(int(cleaned))
    whole, _, fraction = cleaned.partition(".")
    head = spell_integer(int(whole)) if whole else "zero"
    digits = " ".join(_ONES[int(digit)] for digit in fraction)
    return f"{head} point {digits}"


#: Platform shorthand and abbreviations, matched case-insensitively at word
#: boundaries. Expanded rather than spelled out letter by letter, because a
#: narrator saying "ay eye tee ay" immediately sounds like a machine.
_EXPANSIONS: dict[str, str] = {
    "aita": "Am I the asshole",
    "wibta": "Would I be the asshole",
    "yta": "you're the asshole",
    "nta": "not the asshole",
    "esh": "everyone sucks here",
    "nah": "no assholes here",
    "tifu": "today I fucked up",
    "tldr": "in short",
    "tl;dr": "in short",
    "fyi": "for your information",
    "afaik": "as far as I know",
    "iirc": "if I recall correctly",
    "imo": "in my opinion",
    "imho": "in my honest opinion",
    "irl": "in real life",
    "btw": "by the way",
    "idk": "I don't know",
    "ime": "in my experience",
    "op": "the original poster",
    "so": "significant other",
    "dh": "dear husband",
    "dw": "dear wife",
    "sil": "sister in law",
    "mil": "mother in law",
    "fil": "father in law",
    "bil": "brother in law",
    "ex": "ex",
    "eta": "edited to add",
    "nsfw": "not safe for work",
    "diy": "do it yourself",
    "hr": "H R",
    "ceo": "C E O",
    "dm": "direct message",
    "pm": "private message",
}

#: Expansions that are only safe in upper case. "so" and "ex" are ordinary
#: English words; "SO" and "DH" are not. Applying these case-insensitively
#: would rewrite half the corpus into nonsense.
_CASE_SENSITIVE = frozenset({"so", "dh", "dw", "ex", "pm", "dm", "op"})

_AGE_GENDER = re.compile(r"\(?\b(\d{1,2})\s*([MmFf])\b\)?")
_GENDER_AGE = re.compile(r"\(?\b([MmFf])\s*(\d{1,2})\b\)?")
_TIME_MERIDIEM = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?\b")
_HEIGHT = re.compile(r"\b(\d)\s*(?:'|ft\.?|feet)\s*(\d{1,2})?\s*(?:\"|in\.?|inches)?")
#: The magnitude suffix and the space before it are optional *together*, so
#: "$1 and" does not lose the space and become "one dollarand".
_MONEY = re.compile(r"([$€£])\s?(\d[\d,]*(?:\.\d+)?)(?:\s*([kKmM]))?\b")
_PERCENT = re.compile(r"\b(\d[\d,]*(?:\.\d+)?)\s*%")
_ORDINAL = re.compile(r"\b(\d+)(?:st|nd|rd|th)\b", re.IGNORECASE)
_YEAR = re.compile(r"\b(1[1-9]\d{2}|20\d{2})\b")
_NUMBER = re.compile(r"\b\d[\d,]*(?:\.\d+)?\b")
_LETTER_RUN = re.compile(r"\b([A-Z]{2,5})\b")
#: Any run of terminal punctuation collapses to its first mark. Matching a
#: backreference would only catch "!!!" and leave "?!?!", which is the form
#: that actually appears and the one models stumble over.
_MULTI_PUNCT = re.compile(r"([!?.])[!?.]+")
_ELLIPSIS = re.compile(r"\.{3,}|…")
_DASH = re.compile(r"\s*[-–—]{1,3}\s+")  # noqa: RUF001 - matches real en/em dashes
_WHITESPACE = re.compile(r"\s+")
#: Leading quote markers. Greentext (">like this") is meaningful on a board and
#: meaningless read aloud, where it becomes "greater than like this".
_QUOTE_MARKER = re.compile(r"^\s*>+\s*", re.MULTILINE)
_SPACE_BEFORE_PUNCT = re.compile(r"\s+([,.;:!?])")
_REPEATED_COMMA = re.compile(r",\s*(?=[,.])")

_CURRENCY_NAMES = {"$": "dollars", "€": "euros", "£": "pounds"}
_CURRENCY_MINOR = {"$": "cents", "€": "cents", "£": "pence"}
_MAGNITUDE_VALUES = {"k": 1_000, "m": 1_000_000}


def _expand_money(match: re.Match[str]) -> str:
    symbol, amount, magnitude = match.group(1), match.group(2), match.group(3)
    unit = _CURRENCY_NAMES[symbol]

    if magnitude:
        # Multiply out rather than reading the parts: "$2.5k" is two thousand
        # five hundred dollars, not "two point five thousand dollars".
        scaled = float(amount.replace(",", "")) * _MAGNITUDE_VALUES[magnitude.lower()]
        if scaled.is_integer():
            return f"{spell_integer(int(scaled))} {unit}"
        return f"{spell_decimal(f'{scaled:.2f}'.rstrip('0').rstrip('.'))} {unit}"

    whole, _, fraction = amount.replace(",", "").partition(".")
    major = int(whole or 0)
    spoken = spell_integer(major)
    # "one dollars" is worse than the digits were.
    if major == 1:
        unit = unit.rstrip("s")

    # Exactly two decimal places is currency, not a fraction: "$4.50" is four
    # dollars fifty, never "four point five zero dollars".
    if len(fraction) == 2:
        minor = int(fraction)
        if minor == 0:
            return f"{spoken} {unit}"
        minor_unit = _CURRENCY_MINOR[symbol]
        if minor == 1:
            minor_unit = minor_unit.rstrip("s") if minor_unit.endswith("s") else minor_unit
        return f"{spoken} {unit} and {spell_integer(minor)} {minor_unit}"
    if fraction:
        return f"{spell_decimal(amount)} {_CURRENCY_NAMES[symbol]}"
    return f"{spoken} {unit}"


def _expand_time(match: re.Match[str]) -> str:
    hour, minute, meridiem = match.group(1), match.group(2), match.group(3)
    spoken = spell_integer(int(hour))
    if minute and minute != "00":
        spoken += (
            f" oh {_ONES[int(minute)]}" if int(minute) < 10 else f" {spell_integer(int(minute))}"
        )
    return f"{spoken} {meridiem.upper()} M"


def _expand_height(match: re.Match[str]) -> str:
    feet, inches = match.group(1), match.group(2)
    spoken = f"{spell_integer(int(feet))} foot"
    if inches:
        spoken += f" {spell_integer(int(inches))}"
    return spoken


def _expand_age_gender(age: str, gender: str) -> str:
    word = "female" if gender.lower() == "f" else "male"
    return f", {spell_integer(int(age))} {word},"


def _expand_abbreviations(text: str) -> str:
    def replace(match: re.Match[str]) -> str:
        token = match.group(0)
        key = token.lower().rstrip(".")
        expansion = _EXPANSIONS.get(key)
        if expansion is None:
            return token
        if key in _CASE_SENSITIVE and not token.isupper():
            return token
        return expansion

    pattern = re.compile(
        r"\b("
        + "|".join(re.escape(key) for key in sorted(_EXPANSIONS, key=len, reverse=True))
        + r")\b",
        re.IGNORECASE,
    )
    return pattern.sub(replace, text)


def _space_out_acronym(match: re.Match[str]) -> str:
    """Read a leftover capital run as letters, not as a word.

    Runs through `_EXPANSIONS` first, so only genuinely unknown acronyms reach
    here. Spacing them is what stops "NDA" being pronounced "nnn-dah".
    """
    token = match.group(1)
    return " ".join(token)


def to_speech_text(text: str) -> str:
    """Rewrite prose into a form a TTS model narrates correctly.

    Order matters and is deliberate: compound patterns that contain digits
    (ages, times, money, heights) are consumed before the generic number rule
    can reach their digits and spell them out of context.
    """
    if not text or not text.strip():
        return ""

    working = _QUOTE_MARKER.sub("", text)
    # Money first: "$3M" is three million dollars, and the age/gender rule would
    # otherwise claim its "3M" and read it as a thirty-something male.
    working = _MONEY.sub(_expand_money, working)
    working = _PERCENT.sub(lambda m: f"{spell_decimal(m.group(1))} percent", working)
    working = _TIME_MERIDIEM.sub(_expand_time, working)
    working = _HEIGHT.sub(_expand_height, working)
    working = _AGE_GENDER.sub(lambda m: _expand_age_gender(m.group(1), m.group(2)), working)
    working = _GENDER_AGE.sub(lambda m: _expand_age_gender(m.group(2), m.group(1)), working)
    working = _ORDINAL.sub(lambda m: spell_ordinal(int(m.group(1))), working)
    working = _expand_abbreviations(working)
    working = _YEAR.sub(lambda m: spell_year(int(m.group(1))), working)
    working = _NUMBER.sub(lambda m: spell_decimal(m.group(0)), working)
    working = _LETTER_RUN.sub(_space_out_acronym, working)

    working = working.replace("&", " and ")
    working = working.replace("@", " at ")
    working = working.replace("+", " plus ")
    working = working.replace("=", " equals ")
    # A slash between words reads as a pause, not as the word "slash".
    working = re.sub(r"(?<=\w)/(?=\w)", ", ", working)

    # Emphatic punctuation carries no information a synthesiser can use, and
    # "?!?!" produces audible stumbling in most models.
    working = _ELLIPSIS.sub(", ", working)
    working = _MULTI_PUNCT.sub(r"\1", working)
    working = _DASH.sub(", ", working)

    working = _WHITESPACE.sub(" ", working)
    working = _SPACE_BEFORE_PUNCT.sub(r"\1", working)
    working = _REPEATED_COMMA.sub("", working)
    return working.strip(" ,")
