"""Source-agnostic text, URL and fingerprint normalization."""

from pulpmill.normalization.hashing import (
    content_hash,
    hamming_distance,
    simhash64,
    simhash_bands,
    simhash_for_text,
    simhash_from_hex,
    simhash_to_hex,
    url_fingerprint,
)
from pulpmill.normalization.text import (
    clean_text,
    count_words,
    fingerprint_text,
    jaccard,
    paragraphs,
    shingles,
    strip_html,
    strip_markdown,
    tokenize,
    truncate,
)
from pulpmill.normalization.url import canonical_url, is_http_url, join_permalink, normalize_url

__all__ = [
    "canonical_url",
    "clean_text",
    "content_hash",
    "count_words",
    "fingerprint_text",
    "hamming_distance",
    "is_http_url",
    "jaccard",
    "join_permalink",
    "normalize_url",
    "paragraphs",
    "shingles",
    "simhash64",
    "simhash_bands",
    "simhash_for_text",
    "simhash_from_hex",
    "simhash_to_hex",
    "strip_html",
    "strip_markdown",
    "tokenize",
    "truncate",
    "url_fingerprint",
]
