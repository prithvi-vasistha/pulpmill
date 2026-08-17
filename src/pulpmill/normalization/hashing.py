"""Content fingerprinting.

Three fingerprints, one per deduplication layer beyond the source-id check:

* `url_fingerprint`  -- SHA-256 of the normalized URL          (layer 2)
* `content_hash`     -- SHA-256 of the flattened body          (layer 3)
* `simhash64`        -- 64-bit locality-sensitive hash         (layer 4)

All three are deterministic and dependency-free. SimHash in particular is the
cheap, explainable near-duplicate mechanism -- it catches the same story
reposted across platforms with light edits, without an embedding model, a GPU,
or a vector database. Semantic dedup remains a later layer that plugs in behind
the same strategy interface.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Sequence

from pulpmill.normalization.text import fingerprint_text, tokenize
from pulpmill.normalization.url import normalize_url

SIMHASH_BITS = 64
_SIMHASH_MASK = (1 << SIMHASH_BITS) - 1


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def url_fingerprint(url: str) -> str:
    """Dedup layer 2 key: hash of the normalized URL."""
    return sha256_hex(normalize_url(url))


def content_hash(text: str) -> str:
    """Dedup layer 3 key: hash of the flattened body.

    Built from `fingerprint_text`, so reformatting, casing changes and URL
    swaps do not produce a different hash.
    """
    return sha256_hex(fingerprint_text(text))


def title_content_hash(title: str, body: str) -> str:
    """Hash covering title *and* body.

    Useful where a platform reuses body text across differently-titled posts
    (recurring prompt threads); the pipeline stores the body-only hash and this
    one is available to strategies that need the stricter key.
    """
    return sha256_hex(f"{fingerprint_text(title)}\x1f{fingerprint_text(body)}")


def _token_hash(token: str) -> int:
    """Stable 64-bit hash of a token.

    Deliberately not Python's `hash()`: that is randomised per process by
    PYTHONHASHSEED, which would make fingerprints non-reproducible across runs.
    """
    digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    return int.from_bytes(digest, "big")


def simhash64(tokens: Sequence[str]) -> int | None:
    """Charikar SimHash over weighted token frequencies.

    Returns None for an empty token list. Token frequency is used as the weight,
    so a word repeated throughout a story influences the fingerprint more than
    an incidental one.
    """
    if not tokens:
        return None

    vector = [0] * SIMHASH_BITS
    for token, weight in Counter(tokens).items():
        token_hash = _token_hash(token)
        for bit in range(SIMHASH_BITS):
            if token_hash >> bit & 1:
                vector[bit] += weight
            else:
                vector[bit] -= weight

    fingerprint = 0
    for bit in range(SIMHASH_BITS):
        if vector[bit] > 0:
            fingerprint |= 1 << bit
    return fingerprint


def simhash_for_text(text: str) -> int | None:
    """SimHash of prose, via the standard tokenizer."""
    return simhash64(tokenize(text))


def hamming_distance(left: int, right: int) -> int:
    """Number of differing bits between two fingerprints."""
    return ((left ^ right) & _SIMHASH_MASK).bit_count()


def simhash_to_hex(value: int) -> str:
    """Zero-padded 16-char hex, so stored fingerprints sort and compare as text."""
    return format(value & _SIMHASH_MASK, "016x")


def simhash_from_hex(value: str) -> int:
    return int(value, 16) & _SIMHASH_MASK


def simhash_bands(value: int, band_count: int) -> list[str]:
    """Split a fingerprint into equal slices for banded LSH lookup.

    Two fingerprints within a Hamming distance of `d` must agree on at least one
    band whenever `d < band_count` (pigeonhole). That turns near-duplicate
    search from a full table scan into an indexed equality lookup -- which is
    what makes this affordable to run on every ingested story.
    """
    if band_count < 1 or SIMHASH_BITS % band_count:
        raise ValueError(f"band_count must divide {SIMHASH_BITS}")
    width = SIMHASH_BITS // band_count
    mask = (1 << width) - 1
    hex_width = (width + 3) // 4
    return [
        format((value >> (index * width)) & mask, f"0{hex_width}x") for index in range(band_count)
    ]


def combined_hash(parts: Iterable[str]) -> str:
    """Order-sensitive hash of several strings, separated unambiguously."""
    digest = hashlib.sha256()
    for index, part in enumerate(parts):
        if index:
            digest.update(b"\x1f")
        digest.update(part.encode("utf-8"))
    return digest.hexdigest()
