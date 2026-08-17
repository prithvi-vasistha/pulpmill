"""Editorial selection and, above all, its fallback behaviour.

The rule under test: whatever happens to the configured provider, the pipeline
still produces a usable, ordered batch -- and the degradation is recorded rather
than hidden.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from pulpmill.config.models import AppConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.domain.enums import PipelineStage, StoryStatus
from pulpmill.domain.errors import EditorialError, EditorialProviderUnavailableError
from pulpmill.editorial.claude import ClaudeProvider
from pulpmill.editorial.deterministic import DeterministicProvider
from pulpmill.editorial.provider import EditorialCandidate, EditorialDecision, SelectedStory
from pulpmill.editorial.service import EditorialSelector, build_provider
from pulpmill.ranking.engine import RankingEngine


class ExplodingProvider:
    """A provider that always fails, in a configurable way."""

    def __init__(self, error: Exception, *, available: bool = True) -> None:
        self._error = error
        self._available = available

    @property
    def name(self) -> str:
        return "claude"

    def available(self) -> tuple[bool, str]:
        return (True, "ready") if self._available else (False, "no API key configured")

    def select(
        self,
        candidates: Sequence[EditorialCandidate],
        *,
        count: int,
        recently_used_titles: Sequence[str] = (),
    ) -> EditorialDecision:
        raise self._error


class ReversingProvider:
    """A provider that meaningfully reorders, to prove selection is applied."""

    @property
    def name(self) -> str:
        return "claude"

    def available(self) -> tuple[bool, str]:
        return True, "ready"

    def select(
        self,
        candidates: Sequence[EditorialCandidate],
        *,
        count: int,
        recently_used_titles: Sequence[str] = (),
    ) -> EditorialDecision:
        chosen = list(reversed(candidates))[:count]
        return EditorialDecision(
            provider="claude",
            selections=tuple(
                SelectedStory(story_id=c.story_id, position=i, rationale="reversed")
                for i, c in enumerate(chosen, start=1)
            ),
            notes="reversed for the test",
        )


@pytest.fixture
def ranked_stories(config: AppConfig, stories, rankings, make_story, clock) -> list[str]:
    """Five ranked stories with strictly decreasing scores."""
    engine = RankingEngine(config)
    ids: list[str] = []
    for index in range(5):
        story = make_story(
            source_id=f"t3_e{index}",
            canonical_url=f"https://www.reddit.com/r/x/comments/e{index}/",
            score=(index + 1) * 4000,
        )
        stories.upsert(story)
        stories.transition(story.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE)
        stories.transition(story.id, StoryStatus.DEDUPLICATED, stage=PipelineStage.DEDUPLICATE)
        result = engine.rank(story, reference_time=clock.now())
        rankings.save(result)
        stories.transition(story.id, StoryStatus.RANKED, stage=PipelineStage.RANK)
        ids.append(story.id)
    return ids


def selector_with(provider, config, stories, rankings, editorial) -> EditorialSelector:
    return EditorialSelector(
        config=config,
        stories=stories,
        rankings=rankings,
        editorial=editorial,
        provider=provider,
    )


class TestDeterministicProvider:
    def test_it_is_always_available(self) -> None:
        available, _ = DeterministicProvider().available()
        assert available is True

    def test_it_returns_ranking_order(self, config, stories, rankings, editorial, ranked_stories):
        selector = selector_with(DeterministicProvider(), config, stories, rankings, editorial)
        result = selector.select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            count=3,
        )
        assert result is not None
        expected = [
            entry.story.id
            for entry in rankings.top_candidates(
                ranking_version=config.ranking.version,
                config_fingerprint=config.ranking.fingerprint(),
                limit=3,
            )
        ]
        assert list(result.decision.story_ids()) == expected

    def test_positions_are_one_based_and_contiguous(
        self, config, stories, rankings, editorial, ranked_stories
    ) -> None:
        result = selector_with(
            DeterministicProvider(), config, stories, rankings, editorial
        ).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            count=4,
        )
        assert result is not None
        assert [item.position for item in result.decision.selections] == [1, 2, 3, 4]

    def test_it_is_deterministic(self, config, stories, rankings, editorial, ranked_stories):
        provider = DeterministicProvider()
        candidates = [
            EditorialCandidate.from_ranked(entry)
            for entry in rankings.top_candidates(
                ranking_version=config.ranking.version,
                config_fingerprint=config.ranking.fingerprint(),
                limit=5,
            )
        ]
        first = provider.select(candidates, count=3)
        second = provider.select(candidates, count=3)
        assert first.story_ids() == second.story_ids()


class TestSelectionStage:
    def test_selected_stories_move_to_selected(
        self, config, stories, rankings, editorial, ranked_stories
    ) -> None:
        result = selector_with(
            DeterministicProvider(), config, stories, rankings, editorial
        ).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            count=2,
        )
        assert result is not None
        for story_id in result.decision.story_ids():
            assert stories.require(story_id).status is StoryStatus.SELECTED

    def test_the_batch_and_its_ordering_are_persisted(
        self, config, stories, rankings, editorial, ranked_stories
    ) -> None:
        result = selector_with(
            DeterministicProvider(), config, stories, rankings, editorial
        ).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            count=3,
        )
        assert result is not None
        batch = editorial.get_batch(result.batch_id)
        assert batch is not None
        assert batch.effective_provider == "deterministic"
        assert [entry.position for entry in batch.entries] == [1, 2, 3]
        assert batch.candidate_count == 5

    def test_a_provider_reordering_is_actually_applied(
        self, config, stories, rankings, editorial, ranked_stories
    ) -> None:
        deterministic_order = [
            entry.story.id
            for entry in rankings.top_candidates(
                ranking_version=config.ranking.version,
                config_fingerprint=config.ranking.fingerprint(),
                limit=5,
            )
        ]
        result = selector_with(ReversingProvider(), config, stories, rankings, editorial).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            count=5,
        )
        assert result is not None
        assert list(result.decision.story_ids()) == list(reversed(deterministic_order))
        assert result.used_fallback is False

    def test_nothing_ranked_returns_none_rather_than_raising(
        self, config, stories, rankings, editorial
    ) -> None:
        assert (
            selector_with(DeterministicProvider(), config, stories, rankings, editorial).select(
                ranking_version=config.ranking.version,
                config_fingerprint=config.ranking.fingerprint(),
            )
            is None
        )

    def test_asking_for_more_than_exists_selects_what_there_is(
        self, config, stories, rankings, editorial, ranked_stories
    ) -> None:
        result = selector_with(
            DeterministicProvider(), config, stories, rankings, editorial
        ).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            pool_size=100,
            count=50,
        )
        assert result is not None
        assert len(result.decision.selections) == 5

    def test_only_a_small_candidate_set_reaches_the_provider(
        self, config, stories, rankings, editorial, ranked_stories
    ) -> None:
        """Claude must never see the whole scraped dataset."""
        seen: list[int] = []

        class Counting(DeterministicProvider):
            def select(self, candidates, *, count, recently_used_titles=()):  # type: ignore[override]
                seen.append(len(candidates))
                return super().select(
                    candidates, count=count, recently_used_titles=recently_used_titles
                )

        selector_with(Counting(), config, stories, rankings, editorial).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            pool_size=2,
            count=2,
        )
        assert seen == [2]


class TestFallbackOrdering:
    @pytest.mark.parametrize(
        "error",
        [
            EditorialError("request timed out"),
            EditorialError("claude API returned an error status", status_code=529),
            EditorialError("claude declined to answer this request"),
        ],
        ids=["timeout", "api_error", "refusal"],
    )
    def test_any_provider_failure_falls_back_to_ranking_order(
        self, config, stories, rankings, editorial, ranked_stories, error: Exception
    ) -> None:
        expected = [
            entry.story.id
            for entry in rankings.top_candidates(
                ranking_version=config.ranking.version,
                config_fingerprint=config.ranking.fingerprint(),
                limit=3,
            )
        ]
        result = selector_with(
            ExplodingProvider(error), config, stories, rankings, editorial
        ).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            count=3,
        )
        assert result is not None
        assert result.used_fallback is True
        assert result.provider == "claude"
        assert result.effective_provider == "deterministic"
        assert list(result.decision.story_ids()) == expected

    def test_an_unavailable_provider_falls_back_without_calling_it(
        self, config, stories, rankings, editorial, ranked_stories
    ) -> None:
        provider = ExplodingProvider(RuntimeError("must not be called"), available=False)
        result = selector_with(provider, config, stories, rankings, editorial).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            count=2,
        )
        assert result is not None
        assert result.used_fallback is True
        assert "no API key" in (result.fallback_reason or "")

    def test_the_fallback_reason_is_persisted_on_the_batch(
        self, config, stories, rankings, editorial, ranked_stories
    ) -> None:
        """A silent degradation would otherwise vanish with the log rotation."""
        result = selector_with(
            ExplodingProvider(EditorialError("upstream timeout")),
            config,
            stories,
            rankings,
            editorial,
        ).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            count=2,
        )
        assert result is not None
        batch = editorial.get_batch(result.batch_id)
        assert batch is not None
        assert batch.provider == "claude"
        assert batch.effective_provider == "deterministic"
        assert batch.used_fallback is True
        assert "upstream timeout" in (batch.fallback_reason or "")

    def test_stories_are_still_selected_after_a_fallback(
        self, config, stories, rankings, editorial, ranked_stories
    ) -> None:
        result = selector_with(
            ExplodingProvider(EditorialError("boom")), config, stories, rankings, editorial
        ).select(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            count=2,
        )
        assert result is not None
        for story_id in result.decision.story_ids():
            assert stories.require(story_id).status is StoryStatus.SELECTED


class TestProviderConstruction:
    def test_the_default_provider_needs_no_credentials(self, config, secrets) -> None:
        provider = build_provider(config, secrets)
        assert provider.name == "deterministic"
        assert provider.available()[0] is True

    def test_claude_without_a_key_is_unavailable_not_broken(self, config) -> None:
        provider = build_provider(config, SecretStore(environ={}), name="claude")
        available, detail = provider.available()
        assert available is False
        assert "ANTHROPIC_API_KEY" in detail

    def test_claude_with_a_key_reports_ready(self, config) -> None:
        provider = build_provider(
            config, SecretStore(environ={"ANTHROPIC_API_KEY": "sk-test"}), name="claude"
        )
        available, detail = provider.available()
        assert available is True
        assert config.editorial.claude.model in detail

    def test_selecting_without_a_key_raises_a_typed_error(self, config) -> None:
        provider = ClaudeProvider(config.editorial.claude, api_key=None)
        with pytest.raises(EditorialProviderUnavailableError):
            provider.select([], count=1)

    def test_an_unknown_provider_name_falls_back_to_deterministic(self, config, secrets) -> None:
        assert build_provider(config, secrets, name="not-a-provider").name == "deterministic"


class TestCandidateProjection:
    def test_candidates_carry_what_an_editor_needs(
        self, config, stories, rankings, ranked_stories
    ) -> None:
        entry = rankings.top_candidates(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            limit=1,
        )[0]
        candidate = EditorialCandidate.from_ranked(entry)

        assert candidate.story_id == entry.story.id
        assert candidate.canonical_url == entry.story.canonical_url
        assert candidate.estimated_seconds > 0
        assert candidate.age_hours >= 0
        assert candidate.community

    def test_the_excerpt_is_bounded(self, config, stories, rankings, ranked_stories) -> None:
        """Bounds token cost and keeps the provider on selection, not reading."""
        entry = rankings.top_candidates(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            limit=1,
        )[0]
        candidate = EditorialCandidate.from_ranked(entry, excerpt_chars=200)
        assert len(candidate.excerpt) <= 200
        assert "\n" not in candidate.excerpt

    def test_the_prompt_projection_excludes_internal_fields(
        self, config, stories, rankings, ranked_stories
    ) -> None:
        entry = rankings.top_candidates(
            ranking_version=config.ranking.version,
            config_fingerprint=config.ranking.fingerprint(),
            limit=1,
        )[0]
        rendered = EditorialCandidate.from_ranked(entry).to_prompt_dict()
        assert set(rendered) == {
            "story_id",
            "title",
            "source",
            "community",
            "word_count",
            "estimated_narration_seconds",
            "age_hours",
            "local_score",
            "excerpt",
        }
