"""Canonical URL handling.

Two functions with very different contracts:

* `normalize_url`  -- produces a comparison key. Aggressive: drops tracking
                      parameters, folds host aliases, sorts the query.
* `canonical_url`  -- the URL we *store and publish*. Only whitespace-trimmed.

The stored `canonical_url` is never rewritten. A Reddit permalink must come out
of the pipeline byte-identical to what Reddit gave us, because it ends up in a
video description as attribution. Deduplication compares the derived
`url_fingerprint` instead.
"""

from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

#: Query parameters that carry no content identity. Conservative on purpose:
#: stripping a meaningful parameter would merge two distinct stories.
TRACKING_PARAMS = frozenset(
    {
        "utm_source",
        "utm_medium",
        "utm_campaign",
        "utm_term",
        "utm_content",
        "utm_name",
        "fbclid",
        "gclid",
        "msclkid",
        "dclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "ref",
        "ref_source",
        "ref_campaign",
        "referrer",
        "share_id",
        "correlation_id",
        "$deep_link",
        "_branch_match_id",
        "rdt",
    }
)

#: Host prefixes that address the same content. `old.reddit.com/r/x/comments/1`
#: and `www.reddit.com/r/x/comments/1` are one story.
_HOST_PREFIXES = ("www.", "old.", "new.", "np.", "m.", "i.", "amp.")

_DEFAULT_PORTS = {"http": 80, "https": 443}


def canonical_url(url: str) -> str:
    """The URL as stored. Trimmed only -- never rewritten."""
    return url.strip()


def normalize_url(url: str) -> str:
    """Reduce a URL to a stable comparison key.

    Applies, in order: lowercase scheme and host, strip a known host prefix,
    drop the default port, drop tracking parameters, sort the remaining query,
    drop the fragment, and strip a trailing slash.

    A string that does not parse as a URL is returned lowercased and trimmed
    rather than raising -- a bad URL should not crash ingestion.
    """
    text = url.strip()
    if not text:
        return ""

    try:
        parts = urlsplit(text)
    except ValueError:
        return text.lower()

    if not parts.scheme or not parts.netloc:
        return text.lower().rstrip("/")

    scheme = parts.scheme.lower()

    host = (parts.hostname or "").lower()
    for prefix in _HOST_PREFIXES:
        if host.startswith(prefix) and len(host) > len(prefix):
            host = host[len(prefix) :]
            break

    netloc = host
    if parts.port is not None and parts.port != _DEFAULT_PORTS.get(scheme):
        netloc = f"{host}:{parts.port}"

    path = parts.path.rstrip("/") or "/"

    kept = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    query = urlencode(sorted(kept), doseq=True)

    return urlunsplit((scheme, netloc, path, query, ""))


def is_http_url(url: str) -> bool:
    """Whether this is a plain http(s) URL.

    Used to reject `javascript:`, `data:` and similar schemes arriving in
    scraped content before they are ever stored or rendered.
    """
    try:
        parts = urlsplit(url.strip())
    except ValueError:
        return False
    return parts.scheme.lower() in {"http", "https"} and bool(parts.netloc)


def join_permalink(base: str, permalink: str) -> str:
    """Join a site base with a source-supplied permalink path.

    Used where a source returns a path rather than an absolute URL (Reddit's
    `data.permalink`). An already-absolute permalink is returned untouched, so
    the source's own URL always wins.
    """
    permalink = permalink.strip()
    if is_http_url(permalink):
        return permalink
    return f"{base.rstrip('/')}/{permalink.lstrip('/')}"
