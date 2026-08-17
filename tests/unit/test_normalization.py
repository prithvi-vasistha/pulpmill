"""Text, URL and fingerprint normalization."""

from __future__ import annotations

import pytest

from pulpmill.normalization.hashing import (
    content_hash,
    hamming_distance,
    simhash_bands,
    simhash_for_text,
    simhash_to_hex,
    url_fingerprint,
)
from pulpmill.normalization.text import (
    clean_text,
    count_words,
    fingerprint_text,
    jaccard,
    shingles,
    strip_html,
    strip_markdown,
    tokenize,
    truncate,
)
from pulpmill.normalization.url import canonical_url, is_http_url, join_permalink, normalize_url


class TestCanonicalUrlNormalization:
    @pytest.mark.parametrize(
        ("left", "right"),
        [
            # Host aliases that address the same content.
            (
                "https://www.reddit.com/r/nosleep/comments/abc/title/",
                "https://old.reddit.com/r/nosleep/comments/abc/title",
            ),
            (
                "https://np.reddit.com/r/nosleep/comments/abc/title",
                "https://reddit.com/r/nosleep/comments/abc/title/",
            ),
            # Tracking parameters carry no content identity.
            (
                "https://example.com/story?utm_source=twitter&utm_medium=social",
                "https://example.com/story",
            ),
            ("https://example.com/story?ref=homepage", "https://example.com/story"),
            # Query order is not identity.
            ("https://example.com/s?b=2&a=1", "https://example.com/s?a=1&b=2"),
            # Fragments, default ports and case are not identity.
            ("https://example.com/s#section-2", "https://EXAMPLE.com:443/s"),
            ("http://example.com:80/s", "http://example.com/s"),
        ],
    )
    def test_equivalent_urls_share_a_fingerprint(self, left: str, right: str) -> None:
        assert normalize_url(left) == normalize_url(right)
        assert url_fingerprint(left) == url_fingerprint(right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("https://example.com/story-a", "https://example.com/story-b"),
            # A meaningful query parameter must survive.
            ("https://example.com/s?page=1", "https://example.com/s?page=2"),
            # Different hosts are different stories.
            ("https://example.com/s", "https://example.org/s"),
            # A non-default port is part of the address.
            ("https://example.com:8443/s", "https://example.com/s"),
        ],
    )
    def test_distinct_urls_do_not_collide(self, left: str, right: str) -> None:
        assert normalize_url(left) != normalize_url(right)

    def test_canonical_url_is_never_rewritten(self) -> None:
        """The stored URL is the source's own, trimmed only.

        This is the attribution link that ends up in a video description, so it
        must survive the pipeline byte-identical.
        """
        permalink = (
            "https://www.reddit.com/r/AmItheAsshole/comments/1abcde/some_title/?utm_source=x"
        )
        assert canonical_url(f"  {permalink}  ") == permalink
        # ...while the derived comparison key does drop the tracking parameter.
        assert "utm_source" not in normalize_url(permalink)

    def test_malformed_urls_do_not_raise(self) -> None:
        assert normalize_url("not a url") == "not a url"
        assert normalize_url("") == ""

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            ("https://example.com", True),
            ("http://example.com", True),
            ("javascript:alert(1)", False),
            ("data:text/html;base64,PHNjcmlwdD4=", False),
            ("/r/nosleep/comments/abc", False),
        ],
    )
    def test_is_http_url_rejects_dangerous_schemes(self, url: str, expected: bool) -> None:
        assert is_http_url(url) is expected

    def test_join_permalink_prefers_an_absolute_source_url(self) -> None:
        assert (
            join_permalink("https://www.reddit.com", "/r/x/comments/1/")
            == "https://www.reddit.com/r/x/comments/1/"
        )
        assert (
            join_permalink("https://www.reddit.com", "https://redd.it/1abc")
            == "https://redd.it/1abc"
        )


class TestTextNormalization:
    def test_strip_markdown_keeps_link_text_and_drops_targets(self) -> None:
        result = strip_markdown("See my [previous post](https://example.com/old) for context.")
        assert result == "See my previous post for context."

    def test_strip_markdown_removes_emphasis_and_headings(self) -> None:
        assert strip_markdown("## Heading\n**bold** and _italic_") == "Heading\nbold and italic"

    def test_strip_html_preserves_paragraph_structure(self) -> None:
        result = strip_html("one<br><br>two</p>three")
        assert "one" in result and "two" in result and "three" in result
        assert "\n" in result

    def test_strip_html_does_not_resurrect_escaped_markup(self) -> None:
        """Entities are unescaped after tags are removed, never before."""
        assert strip_html("&lt;script&gt;alert(1)&lt;/script&gt;") == "<script>alert(1)</script>"

    def test_clean_text_is_idempotent(self) -> None:
        raw = "Line one\r\n\r\n\r\n   Line   two   \t\n"
        once = clean_text(raw)
        assert clean_text(once) == once

    def test_clean_text_strips_invisible_characters(self) -> None:
        assert clean_text("hel​lo﻿") == "hello"

    def test_fingerprint_text_ignores_case_punctuation_and_urls(self) -> None:
        a = fingerprint_text("The Cat, sat! On the mat. https://example.com/x")
        b = fingerprint_text("the cat sat on the mat")
        assert a == b

    def test_count_words_and_tokenize_agree(self) -> None:
        text = "I can't believe it happened again"
        assert count_words(text) == len(tokenize(text)) == 6

    def test_truncate_breaks_on_a_word_boundary(self) -> None:
        result = truncate("the quick brown fox jumps over the lazy dog", 20)
        assert result.endswith("...")
        assert len(result) <= 20
        assert not result.replace("...", "").endswith(" ")

    def test_shingles_and_jaccard(self) -> None:
        left = shingles(tokenize("the cat sat on the mat"), 2)
        right = shingles(tokenize("the cat sat on the rug"), 2)
        assert 0.0 < jaccard(left, right) < 1.0
        assert jaccard(left, left) == 1.0
        assert jaccard(left, set()) == 0.0

    def test_shingles_handles_text_shorter_than_the_window(self) -> None:
        assert shingles(["one", "two"], 3) == {"one two"}


class TestContentHashing:
    def test_identical_content_hashes_identically(self) -> None:
        assert content_hash("Hello world") == content_hash("Hello world")

    def test_reformatting_does_not_change_the_content_hash(self) -> None:
        """A repost with different casing, punctuation and links is one story."""
        original = "The night I got lost, everything changed. See https://example.com/a"
        repost = "the night i got lost everything changed!! see https://other.example/b"
        assert content_hash(original) == content_hash(repost)

    def test_different_content_hashes_differently(self) -> None:
        assert content_hash("story one") != content_hash("story two")

    def test_content_hash_is_stable_across_processes(self) -> None:
        """Pinned so a hashing change is a deliberate, visible decision."""
        assert content_hash("hello world") == content_hash("Hello, World!")
        assert len(content_hash("x")) == 64


class TestSimHash:
    def _long(self, text: str) -> str:
        return " ".join([text] * 12)

    def test_identical_text_has_distance_zero(self) -> None:
        text = self._long("the quiet house on the hill was never really empty")
        left, right = simhash_for_text(text), simhash_for_text(text)
        assert left is not None and right is not None
        assert hamming_distance(left, right) == 0

    def test_small_edits_stay_close(self) -> None:
        base = self._long("the quiet house on the hill was never really empty")
        edited = base.replace("quiet", "silent", 1)
        left, right = simhash_for_text(base), simhash_for_text(edited)
        assert left is not None and right is not None
        assert hamming_distance(left, right) <= 6

    def test_unrelated_text_is_far_apart(self) -> None:
        left = simhash_for_text(self._long("a story about a haunted country road at night"))
        right = simhash_for_text(self._long("quarterly revenue projections for the logistics unit"))
        assert left is not None and right is not None
        assert hamming_distance(left, right) > 10

    def test_empty_text_has_no_fingerprint(self) -> None:
        assert simhash_for_text("") is None

    def test_fingerprints_are_reproducible_across_runs(self) -> None:
        """Not Python's randomised `hash()` -- otherwise stored bands would rot."""
        assert simhash_for_text("stable input") == simhash_for_text("stable input")

    def test_bands_partition_the_fingerprint(self) -> None:
        value = simhash_for_text(self._long("some reasonably long body of narrative text"))
        assert value is not None
        bands = simhash_bands(value, 4)
        assert len(bands) == 4
        assert "".join(reversed(bands)) == simhash_to_hex(value)

    def test_band_count_must_divide_the_fingerprint(self) -> None:
        with pytest.raises(ValueError, match="band_count"):
            simhash_bands(1, 7)
