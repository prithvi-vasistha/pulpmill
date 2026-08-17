"""The ranking engine and its signals."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from pulpmill.config.models import AppConfig
from pulpmill.domain.ranking import SignalScore
from pulpmill.domain.story import Engagement
from pulpmill.persistence.repositories.stories import NoveltyEntry
from pulpmill.ranking.engine import SCORE_SCALE, RankingEngine
from pulpmill.ranking.signals import (
    CommentActivitySignal,
    EngagementSignal,
    LengthSignal,
    NarrativeSuitabilitySignal,
    NoveltySignal,
    RecencySignal,
    ScoringContext,
    SourceQualitySignal,
    default_signals,
)

REFERENCE = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def context(config: AppConfig, story, *, corpus=()) -> ScoringContext:
    return ScoringContext(
        story=story, config=config, reference_time=REFERENCE, novelty_corpus=corpus
    )


class TestSignalScore:
    def test_out_of_range_values_are_rejected(self) -> None:
        with pytest.raises(ValueError, match="out-of-range"):
            SignalScore(name="bad", value=1.5)
        with pytest.raises(ValueError, match="out-of-range"):
            SignalScore(name="bad", value=-0.1)


class TestEngagementSignal:
    def test_score_at_the_reference_value_is_about_half(self, config, make_story) -> None:
        reference = config.engagement_references("reddit")
        story = make_story(score=int(reference.score_reference), comments=None)
        result = EngagementSignal().score(context(config, story))
        assert result.value == pytest.approx(0.5, abs=0.01)

    def test_more_engagement_scores_higher(self, config, make_story) -> None:
        low = EngagementSignal().score(context(config, make_story(score=100, comments=10)))
        high = EngagementSignal().score(context(config, make_story(score=50_000, comments=5_000)))
        assert high.value > low.value

    def test_a_platform_without_a_score_drops_that_axis(self, config, make_story) -> None:
        """4chan has no score. It must not be scored zero for that."""
        story = make_story(platform="fourchan", score=None, comments=80, quality_key="x")
        result = EngagementSignal().score(context(config, story))
        assert result.available is True
        assert "score" not in result.detail.get("axes", {})
        assert result.value == pytest.approx(0.5, abs=0.01)

    def test_a_platform_reporting_nothing_marks_the_signal_unavailable(
        self, config, make_story
    ) -> None:
        story = make_story(platform="fourchan", score=None, comments=None, quality_key="x")
        result = EngagementSignal().score(context(config, story))
        assert result.available is False

    def test_zero_engagement_scores_zero_not_an_error(self, config, make_story) -> None:
        result = EngagementSignal().score(context(config, make_story(score=0, comments=0)))
        assert result.value == 0.0


class TestRecencySignal:
    def test_a_brand_new_story_scores_one(self, config, make_story) -> None:
        story = make_story(created_at=REFERENCE)
        assert RecencySignal().score(context(config, story)).value == pytest.approx(1.0)

    def test_one_half_life_scores_one_half(self, config, make_story) -> None:
        half_life = config.ranking.recency.half_life_hours
        story = make_story(created_at=REFERENCE - timedelta(hours=half_life))
        assert RecencySignal().score(context(config, story)).value == pytest.approx(0.5)

    def test_beyond_max_age_scores_exactly_zero(self, config, make_story) -> None:
        story = make_story(
            created_at=REFERENCE - timedelta(hours=config.ranking.recency.max_age_hours + 1)
        )
        result = RecencySignal().score(context(config, story))
        assert result.value == 0.0
        assert "older than" in result.detail["reason"]

    def test_a_future_timestamp_is_clamped_rather_than_crashing(self, config, make_story) -> None:
        story = make_story(created_at=REFERENCE + timedelta(hours=5))
        assert RecencySignal().score(context(config, story)).value == pytest.approx(1.0)

    def test_recency_decreases_monotonically_with_age(self, config, make_story) -> None:
        values = [
            RecencySignal()
            .score(context(config, make_story(created_at=REFERENCE - timedelta(hours=hours))))
            .value
            for hours in (0, 12, 36, 100, 300)
        ]
        assert values == sorted(values, reverse=True)


class TestLengthSignal:
    def test_the_ideal_band_scores_one(self, config, make_story) -> None:
        cfg = config.ranking.length
        story = make_story(body="word " * ((cfg.ideal_min_words + cfg.ideal_max_words) // 2))
        assert LengthSignal().score(context(config, story)).value == 1.0

    @pytest.mark.parametrize("words", [10, 5000])
    def test_outside_the_usable_range_scores_zero(self, config, make_story, words: int) -> None:
        story = make_story(body="word " * words)
        assert LengthSignal().score(context(config, story)).value == 0.0

    def test_the_ramps_are_partial_credit(self, config, make_story) -> None:
        cfg = config.ranking.length
        midpoint = (cfg.floor_words + cfg.ideal_min_words) // 2
        result = LengthSignal().score(context(config, make_story(body="word " * midpoint)))
        assert 0.0 < result.value < 1.0
        assert result.detail["band"] == "ramp_up"

    def test_narration_estimate_is_reported(self, config, make_story) -> None:
        result = LengthSignal().score(context(config, make_story(body="word " * 450)))
        assert result.detail["estimated_narration_seconds"] == pytest.approx(180.0, abs=1.0)


class TestCommentActivitySignal:
    def test_a_faster_discussion_scores_higher(self, config, make_story) -> None:
        fast = make_story(comments=500, created_at=REFERENCE - timedelta(hours=2))
        slow = make_story(comments=500, created_at=REFERENCE - timedelta(hours=200))
        engine = CommentActivitySignal()
        assert engine.score(context(config, fast)).value > engine.score(context(config, slow)).value

    def test_missing_comment_counts_mark_it_unavailable(self, config, make_story) -> None:
        story = make_story(comments=None)
        assert CommentActivitySignal().score(context(config, story)).available is False

    def test_very_new_stories_do_not_produce_absurd_rates(self, config, make_story) -> None:
        """Without a floor, a 1-minute-old post with 10 comments looks viral."""
        story = make_story(comments=10, created_at=REFERENCE - timedelta(seconds=30))
        result = CommentActivitySignal().score(context(config, story))
        assert result.detail["effective_age_hours"] >= config.ranking.comment_activity.min_age_hours


class TestNoveltySignal:
    def test_an_empty_corpus_means_everything_is_novel(self, config, make_story) -> None:
        result = NoveltySignal().score(context(config, make_story(), corpus=()))
        assert result.value == 1.0

    def test_a_near_identical_story_in_the_corpus_lowers_novelty(self, config, make_story) -> None:
        story = make_story()
        corpus = (
            NoveltyEntry(
                story_id="other",
                title=story.title,
                content_prefix=story.normalized_content[:1200],
            ),
        )
        assert NoveltySignal().score(context(config, story, corpus=corpus)).value < 0.2

    def test_an_unrelated_corpus_keeps_novelty_high(self, config, make_story) -> None:
        corpus = (
            NoveltyEntry(
                story_id="other",
                title="Quarterly logistics review",
                content_prefix="Freight throughput improved across the northern corridor.",
            ),
        )
        assert NoveltySignal().score(context(config, make_story(), corpus=corpus)).value > 0.8

    def test_a_story_never_competes_with_itself(self, config, make_story) -> None:
        story = make_story()
        corpus = (
            NoveltyEntry(
                story_id=story.id, title=story.title, content_prefix=story.normalized_content
            ),
        )
        assert NoveltySignal().score(context(config, story, corpus=corpus)).value == 1.0

    def test_too_short_to_compare_is_unavailable(self, config, make_story) -> None:
        story = make_story(title="Hi", body="Too short.")
        assert NoveltySignal().score(context(config, story)).available is False


class TestNarrativeSuitabilitySignal:
    def test_a_first_person_story_beats_a_link_dump(self, config, make_story) -> None:
        engine = NarrativeSuitabilitySignal()
        story = make_story(title="AITA for telling my roommate to move out?")
        links = make_story(
            title="Links",
            body=" ".join(f"https://example.com/{n}" for n in range(60)),
        )
        assert (
            engine.score(context(config, story)).value > engine.score(context(config, links)).value
        )

    def test_meta_posts_are_penalised(self, config, make_story) -> None:
        engine = NarrativeSuitabilitySignal()
        plain = make_story()
        meta = make_story(body="This is my first post here. " + plain.normalized_content)
        assert engine.score(context(config, meta)).detail["penalties"]["meta_post"] == 1.0
        assert (
            engine.score(context(config, meta)).value < engine.score(context(config, plain)).value
        )

    def test_shouting_is_penalised(self, config, make_story) -> None:
        engine = NarrativeSuitabilitySignal()
        plain = make_story()
        shouty = make_story(body=plain.normalized_content.upper())
        assert (
            engine.score(context(config, shouty)).value < engine.score(context(config, plain)).value
        )

    def test_evidence_is_reported_for_every_cue(self, config, make_story) -> None:
        result = NarrativeSuitabilitySignal().score(context(config, make_story()))
        assert set(result.detail["cues"]) == {
            "first_person",
            "dialogue",
            "conflict",
            "temporal_structure",
            "paragraph_structure",
            "title_hook",
        }
        assert result.detail["evidence"]["tokens"] > 0

    def test_empty_content_scores_zero_without_crashing(self, config, make_story) -> None:
        story = make_story(body="")
        assert NarrativeSuitabilitySignal().score(context(config, story)).value == 0.0


class TestSourceQualitySignal:
    def test_the_override_for_a_community_is_applied(self, config, make_story) -> None:
        story = make_story(quality_key="nosleep")
        result = SourceQualitySignal().score(context(config, story))
        assert result.value == 1.0
        assert result.detail["override_applied"] is True

    def test_an_unlisted_community_falls_back_to_the_platform_baseline(
        self, config, make_story
    ) -> None:
        story = make_story(quality_key="some_random_sub")
        result = SourceQualitySignal().score(context(config, story))
        assert result.value == config.sources["reddit"].quality
        assert result.detail["override_applied"] is False


class TestRankingEngine:
    def test_scores_land_on_the_zero_to_one_hundred_scale(self, config, make_story) -> None:
        result = RankingEngine(config).rank(make_story(), reference_time=REFERENCE)
        assert 0.0 <= result.final_score <= SCORE_SCALE

    def test_ranking_is_deterministic(self, config, make_story) -> None:
        """Same story, config, version and reference time -> same score."""
        story = make_story()
        engine = RankingEngine(config)
        first = engine.rank(story, reference_time=REFERENCE)
        second = engine.rank(story, reference_time=REFERENCE)
        assert first.final_score == second.final_score
        assert dict(first.component_scores) == dict(second.component_scores)

    def test_a_separate_engine_instance_produces_the_same_score(self, config, make_story) -> None:
        story = make_story()
        assert (
            RankingEngine(config).rank(story, reference_time=REFERENCE).final_score
            == RankingEngine(config).rank(story, reference_time=REFERENCE).final_score
        )

    def test_the_reference_time_is_recorded_for_reproducibility(self, config, make_story) -> None:
        result = RankingEngine(config).rank(make_story(), reference_time=REFERENCE)
        assert result.reference_time == REFERENCE

    def test_changing_a_weight_changes_the_score_and_the_fingerprint(
        self, config, make_story
    ) -> None:
        story = make_story()
        baseline = RankingEngine(config).rank(story, reference_time=REFERENCE)

        weights = config.ranking.weights.model_copy(update={"engagement": 1.0, "recency": 0.0})
        tweaked = config.model_copy(
            update={"ranking": config.ranking.model_copy(update={"weights": weights})}
        )
        changed = RankingEngine(tweaked).rank(story, reference_time=REFERENCE)

        assert changed.config_fingerprint != baseline.config_fingerprint
        assert changed.final_score != baseline.final_score

    def test_weights_are_normalized_so_they_need_not_sum_to_one(self, config, make_story) -> None:
        doubled = config.ranking.weights.model_copy(
            update={name: value * 2 for name, value in config.ranking.weights.as_mapping().items()}
        )
        scaled = config.model_copy(
            update={"ranking": config.ranking.model_copy(update={"weights": doubled})}
        )
        story = make_story()
        assert RankingEngine(scaled).rank(story, reference_time=REFERENCE).final_score == (
            pytest.approx(RankingEngine(config).rank(story, reference_time=REFERENCE).final_score)
        )

    def test_unavailable_signal_weight_is_redistributed(self, config, make_story) -> None:
        """A 4chan story must not be penalised for having no score field."""
        story = make_story(platform="fourchan", score=None, comments=80, quality_key="x")
        result = RankingEngine(config).rank(story, reference_time=REFERENCE)
        weights = dict(result.effective_weights)
        applied = {name: value for name, value in weights.items() if value > 0}
        assert sum(applied.values()) == pytest.approx(1.0)
        assert "comment_activity" in applied

    def test_the_explanation_accounts_for_every_signal(self, config, make_story) -> None:
        result = RankingEngine(config).rank(make_story(), reference_time=REFERENCE)
        signals = result.explanation["signals"]
        assert set(signals) == {signal.name for signal in default_signals()}
        for detail in signals.values():
            assert {"value", "available", "effective_weight", "contribution", "detail"} <= set(
                detail
            )

    def test_contributions_sum_to_the_final_score(self, config, make_story) -> None:
        result = RankingEngine(config).rank(make_story(), reference_time=REFERENCE)
        total = sum(entry["contribution"] for entry in result.explanation["signals"].values())
        assert total == pytest.approx(result.final_score, abs=0.01)

    def test_a_signal_without_a_configured_weight_is_rejected(self, config) -> None:
        class Rogue:
            name = "not_configured"

            def score(self, context: ScoringContext) -> SignalScore:  # pragma: no cover
                return SignalScore(name=self.name, value=1.0)

        with pytest.raises(ValueError, match="not_configured"):
            RankingEngine(config, signals=[Rogue()])

    def test_a_stronger_story_outranks_a_weaker_one(self, config, make_story) -> None:
        strong = make_story(
            title="AITA for telling my roommate to move out after months of unpaid rent?",
            score=25_000,
            comments=4_000,
            created_at=REFERENCE - timedelta(hours=6),
            quality_key="AmItheAsshole",
        )
        weak = make_story(
            title="update",
            body="short post " * 30,
            score=5,
            comments=1,
            created_at=REFERENCE - timedelta(hours=300),
            quality_key="some_random_sub",
        )
        engine = RankingEngine(config)
        assert (
            engine.rank(strong, reference_time=REFERENCE).final_score
            > engine.rank(weak, reference_time=REFERENCE).final_score
        )

    def test_engagement_none_is_not_treated_as_zero(self, config, make_story) -> None:
        """The distinction the whole per-platform design rests on."""
        reported_zero = make_story(score=0, comments=0)
        not_reported = make_story(platform="fourchan", score=None, comments=None, quality_key="x")
        engine = EngagementSignal()
        assert engine.score(context(config, reported_zero)).available is True
        assert engine.score(context(config, not_reported)).available is False

    def test_engagement_object_round_trips(self) -> None:
        engagement = Engagement(score=10, comments=None, views=99)
        assert Engagement.from_dict(engagement.to_dict()) == engagement
