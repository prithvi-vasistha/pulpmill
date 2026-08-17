"""Text normalization.

Two different outputs, for two different jobs:

* `clean_text`       -- reading-quality prose. This is what gets stored as
                        `normalized_content` and eventually handed to TTS, so it
                        keeps case, punctuation and paragraph breaks.
* `fingerprint_text` -- an aggressively flattened form used only for hashing and
                        similarity. Never displayed, never narrated.

Source text is untrusted input. It is stripped and escaped, never evaluated,
never interpolated into a shell command or SQL string.
"""

from __future__ import annotations

import html
import re
import unicodedata

#: Zero-width and bidirectional control characters, written as escapes so this
#: pattern stays readable and diffable. They are invisible in a terminal but
#: they defeat hashing and can be used to smuggle content past filters.
_INVISIBLE_CHARS = re.compile(
    "["
    "​-‏"  # zero-width space/joiners, LTR/RTL marks
    "‪-‮"  # bidirectional embedding/override
    "⁠-⁤"  # word joiner, invisible operators
    "﻿"  # byte-order mark
    "]"
)

_HTML_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_HTML_BLOCK_END = re.compile(r"</(p|div|blockquote|li)>", re.IGNORECASE)
_HTML_TAG = re.compile(r"<[^>]+>")

_MARKDOWN_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MARKDOWN_IMAGE = re.compile(r"!\[([^\]]*)\]\([^)]*\)")
_MARKDOWN_EMPHASIS = re.compile(r"(\*{1,3}|_{1,3})(\S(?:.*?\S)?)\1", re.DOTALL)
_MARKDOWN_HEADING = re.compile(r"^\s{0,3}#{1,6}\s*", re.MULTILINE)
_MARKDOWN_QUOTE = re.compile(r"^\s{0,3}>\s?", re.MULTILINE)
_MARKDOWN_RULE = re.compile(r"^\s{0,3}(?:[-*_]\s*){3,}$", re.MULTILINE)
_MARKDOWN_CODE_FENCE = re.compile(r"```.*?```", re.DOTALL)
_MARKDOWN_INLINE_CODE = re.compile(r"`([^`]+)`")
_MARKDOWN_ESCAPE = re.compile(r"\\([\\`*_{}\[\]()#+\-.!>~|])")

URL_PATTERN = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)

_TRAILING_SPACES = re.compile(r"[ \t]+$", re.MULTILINE)
_HORIZONTAL_RUN = re.compile(r"[ \t]{2,}")
_BLANK_LINE_RUN = re.compile(r"\n{3,}")

_WORD_PATTERN = re.compile(r"[a-z0-9']+")
_NON_FINGERPRINT = re.compile(r"[^a-z0-9\s]+")
_WHITESPACE_RUN = re.compile(r"\s+")


def strip_html(text: str) -> str:
    """Convert an HTML fragment to plain text.

    Block-level closes become newlines so paragraph structure survives, which
    the narrative-suitability signal reads and TTS eventually needs. Entities
    are unescaped *after* tags are removed, so an escaped `&lt;b&gt;` in the
    source cannot reappear as a live tag.
    """
    if not text:
        return ""
    working = _HTML_BR.sub("\n", text)
    working = _HTML_BLOCK_END.sub("\n\n", working)
    working = _HTML_TAG.sub("", working)
    return html.unescape(working)


def strip_markdown(text: str) -> str:
    """Reduce Markdown to the prose inside it.

    Link text is kept and the target dropped: a narrator reads "my old post",
    not a URL. Code fences are removed entirely -- they never narrate well.
    """
    if not text:
        return ""
    working = _MARKDOWN_CODE_FENCE.sub(" ", text)
    working = _MARKDOWN_IMAGE.sub(r"\1", working)
    working = _MARKDOWN_LINK.sub(r"\1", working)
    working = _MARKDOWN_INLINE_CODE.sub(r"\1", working)
    working = _MARKDOWN_HEADING.sub("", working)
    working = _MARKDOWN_QUOTE.sub("", working)
    working = _MARKDOWN_RULE.sub("", working)
    working = _MARKDOWN_EMPHASIS.sub(r"\2", working)
    working = _MARKDOWN_ESCAPE.sub(r"\1", working)
    return working


def clean_text(text: str, *, markdown: bool = False, html_source: bool = False) -> str:
    """Produce reading-quality prose from a source body.

    Idempotent: cleaning an already-clean string returns it unchanged, which
    matters because `renormalize` may run over stored content more than once.
    """
    if not text:
        return ""
    working = text
    if html_source:
        working = strip_html(working)
    if markdown:
        working = strip_markdown(working)

    # NFKC folds look-alike Unicode (fullwidth Latin, ligatures) into ASCII-ish
    # forms so two visually identical posts hash identically.
    working = unicodedata.normalize("NFKC", working)
    working = _INVISIBLE_CHARS.sub("", working)
    working = working.replace("\r\n", "\n").replace("\r", "\n")
    working = _HORIZONTAL_RUN.sub(" ", working)
    working = _TRAILING_SPACES.sub("", working)
    working = _BLANK_LINE_RUN.sub("\n\n", working)
    return working.strip()


def fingerprint_text(text: str) -> str:
    """Flatten text to its content-identity form.

    Case, punctuation and whitespace layout are discarded so that a repost with
    a reformatted body still hashes to the same value.
    """
    if not text:
        return ""
    working = unicodedata.normalize("NFKC", text).lower()
    working = _INVISIBLE_CHARS.sub("", working)
    working = URL_PATTERN.sub(" ", working)
    working = _NON_FINGERPRINT.sub(" ", working)
    return _WHITESPACE_RUN.sub(" ", working).strip()


def tokenize(text: str) -> list[str]:
    """Split into lowercase word tokens. Input for SimHash and shingling."""
    return _WORD_PATTERN.findall(unicodedata.normalize("NFKC", text).lower())


def count_words(text: str) -> int:
    """Word count of prose, used by the length signal and duration estimates."""
    return len(tokenize(text))


def shingles(tokens: list[str], size: int) -> set[str]:
    """Overlapping n-grams, used by the novelty signal.

    A token list shorter than `size` degrades to the single joined token rather
    than returning an empty set, so short texts still compare meaningfully.
    """
    if size < 1:
        raise ValueError("shingle size must be at least 1")
    if not tokens:
        return set()
    if len(tokens) < size:
        return {" ".join(tokens)}
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def jaccard(left: set[str], right: set[str]) -> float:
    """Set overlap in [0, 1]. Two empty sets are defined as identical."""
    if not left and not right:
        return 1.0
    if not left or not right:
        return 0.0
    intersection = len(left & right)
    if intersection == 0:
        return 0.0
    return intersection / len(left | right)


def count_urls(text: str) -> int:
    return len(URL_PATTERN.findall(text))


def paragraphs(text: str) -> list[str]:
    """Non-empty paragraphs, split on blank lines."""
    return [block.strip() for block in text.split("\n\n") if block.strip()]


def truncate(text: str, limit: int, *, suffix: str = "...") -> str:
    """Shorten for display, breaking on a word boundary where possible."""
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= len(suffix):
        return text[:limit]
    cut = text[: limit - len(suffix)]
    space = cut.rfind(" ")
    if space > limit // 2:
        cut = cut[:space]
    return cut.rstrip() + suffix
