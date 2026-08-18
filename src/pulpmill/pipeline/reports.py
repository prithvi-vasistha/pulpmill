"""Structured results returned by pipeline stages.

Returned rather than printed, so the CLI decides on presentation and tests can
assert on numbers instead of scraping stdout.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SourceReport:
    """What one source contributed to a run."""

    platform: str
    available: bool = True
    detail: str = ""
    remediation: str | None = None

    fetched: int = 0
    #: Valid records that are simply not usable as narration (too short,
    #: removed, link-only). Not errors.
    filtered: int = 0
    #: Subset of `filtered` refused by content policy rather than by an
    #: adapter's own filters. Counted separately: "we chose not to ingest this"
    #: and "this did not meet the quality bar" are different facts.
    blocked: int = 0
    new: int = 0
    #: Already-held posts that were refreshed rather than re-added.
    known: int = 0
    duplicates: int = 0
    failures: int = 0
    duration_seconds: float = 0.0

    @property
    def normalized(self) -> int:
        return self.new + self.known + self.duplicates

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "available": self.available,
            "detail": self.detail,
            "fetched": self.fetched,
            "filtered": self.filtered,
            "blocked": self.blocked,
            "normalized": self.normalized,
            "new": self.new,
            "known": self.known,
            "duplicates": self.duplicates,
            "failures": self.failures,
            "duration_seconds": round(self.duration_seconds, 3),
        }


@dataclass(slots=True)
class IngestReport:
    job_id: str
    sources: dict[str, SourceReport] = field(default_factory=dict)

    @property
    def fetched(self) -> int:
        return sum(report.fetched for report in self.sources.values())

    @property
    def new(self) -> int:
        return sum(report.new for report in self.sources.values())

    @property
    def known(self) -> int:
        return sum(report.known for report in self.sources.values())

    @property
    def duplicates(self) -> int:
        return sum(report.duplicates for report in self.sources.values())

    @property
    def filtered(self) -> int:
        return sum(report.filtered for report in self.sources.values())

    @property
    def blocked(self) -> int:
        return sum(report.blocked for report in self.sources.values())

    @property
    def failures(self) -> int:
        return sum(report.failures for report in self.sources.values())

    @property
    def skipped_sources(self) -> list[str]:
        return [name for name, report in self.sources.items() if not report.available]

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "fetched": self.fetched,
            "new": self.new,
            "known": self.known,
            "duplicates": self.duplicates,
            "filtered": self.filtered,
            "blocked": self.blocked,
            "failures": self.failures,
            "sources": {name: report.as_dict() for name, report in self.sources.items()},
        }


@dataclass(slots=True)
class RankReport:
    job_id: str
    ranking_version: str
    config_fingerprint: str
    reference_time: str
    considered: int = 0
    ranked: int = 0
    #: Already scored under this exact version and configuration.
    skipped: int = 0
    failures: int = 0
    corpus_size: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "ranking_version": self.ranking_version,
            "config_fingerprint": self.config_fingerprint,
            "reference_time": self.reference_time,
            "considered": self.considered,
            "ranked": self.ranked,
            "skipped": self.skipped,
            "failures": self.failures,
            "corpus_size": self.corpus_size,
        }


@dataclass(slots=True)
class RunReport:
    """A full `fetch -> normalize -> dedupe -> persist -> rank` pass."""

    ingest: IngestReport
    rank: RankReport

    def as_dict(self) -> dict[str, Any]:
        return {"ingest": self.ingest.as_dict(), "rank": self.rank.as_dict()}
