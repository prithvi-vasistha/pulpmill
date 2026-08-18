"""The content-policy blocklist.

See docs/CONTENT_POLICY.md for the reasoning. These tests pin the mechanism:
the blocklist is generic across sources, case-insensitive, and enforced by the
core rather than by any adapter.
"""

from __future__ import annotations

from pulpmill.config.models import AppConfig, SourceConfig


def source(**overrides: object) -> SourceConfig:
    defaults: dict[str, object] = {"adapter": "reddit"}
    defaults.update(overrides)
    return SourceConfig(**defaults)  # type: ignore[arg-type]


class TestBlocklistMatching:
    def test_a_listed_community_is_blocked(self) -> None:
        assert source(blocked_quality_keys=("nosleep",)).is_blocked("nosleep")

    def test_an_unlisted_community_is_allowed(self) -> None:
        assert not source(blocked_quality_keys=("nosleep",)).is_blocked("confession")

    def test_matching_is_case_insensitive(self) -> None:
        """Reddit treats r/NoSleep and r/nosleep as the same subreddit.

        A blocklist that a capitalisation defeats is not a blocklist.
        """
        blocked = source(blocked_quality_keys=("nosleep",))
        assert blocked.is_blocked("NoSleep")
        assert blocked.is_blocked("NOSLEEP")

    def test_an_unknown_community_is_allowed(self) -> None:
        assert not source(blocked_quality_keys=("nosleep",)).is_blocked(None)

    def test_an_empty_blocklist_blocks_nothing(self) -> None:
        assert not source().is_blocked("anything")


class TestShippedPolicy:
    def test_the_creative_fiction_communities_are_blocked(self, config: AppConfig) -> None:
        """Original fiction whose authors retain and enforce rights."""
        reddit = config.sources["reddit"]
        assert reddit.is_blocked("nosleep")
        assert reddit.is_blocked("LetsNotMeet")

    def test_blocked_communities_are_absent_from_the_query_list(self, config: AppConfig) -> None:
        """The blocklist is a safety net, not a substitute for not asking."""
        reddit = config.sources["reddit"]
        queried = {str(query.get("subreddit", "")).casefold() for query in reddit.queries}
        for blocked in reddit.blocked_quality_keys:
            assert blocked.casefold() not in queried

    def test_blocked_communities_carry_no_quality_override(self, config: AppConfig) -> None:
        """A quality score for a community we refuse to ingest is dead config."""
        reddit = config.sources["reddit"]
        for blocked in reddit.blocked_quality_keys:
            assert blocked not in reddit.quality_overrides

    def test_the_anecdote_communities_are_still_allowed(self, config: AppConfig) -> None:
        reddit = config.sources["reddit"]
        assert not reddit.is_blocked("AmItheAsshole")
        assert not reddit.is_blocked("TrueOffMyChest")

    def test_the_mechanism_is_generic_across_sources(self, config: AppConfig) -> None:
        """4chan boards go through the same check, with no adapter branching."""
        fourchan = config.sources["fourchan"]
        assert fourchan.is_blocked("x") is False
        assert hasattr(fourchan, "blocked_quality_keys")
