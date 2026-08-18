"""Layered deduplication against real stored state."""

from __future__ import annotations

import pytest

from pulpmill.config.models import AppConfig
from pulpmill.deduplication.engine import DeduplicationEngine, DedupOutcome
from pulpmill.domain.enums import DedupLayer, PipelineStage, StoryStatus
from pulpmill.persistence.repositories.stories import StoryRepository


def engine_for(
    config: AppConfig, stories: StoryRepository, **layer_overrides
) -> DeduplicationEngine:
    layers = config.deduplication.layers
    if layer_overrides:
        layers = layers.model_copy(update=layer_overrides)
    return DeduplicationEngine(config.deduplication.model_copy(update={"layers": layers}), stories)


class TestLayerOne:
    def test_a_brand_new_story_is_new(self, config, stories, make_story) -> None:
        assert engine_for(config, stories).evaluate(make_story()).outcome is DedupOutcome.NEW

    def test_the_same_post_again_is_known_not_a_duplicate(
        self, config, stories, make_story
    ) -> None:
        """Same source pair means the same row, not a second story."""
        story = make_story()
        stories.upsert(story)
        verdict = engine_for(config, stories).evaluate(story)
        assert verdict.outcome is DedupOutcome.KNOWN
        assert verdict.layer is DedupLayer.EXACT_SOURCE
        assert verdict.original_id == story.id

    def test_the_same_id_on_another_platform_is_not_a_match(
        self, config, stories, make_story
    ) -> None:
        stories.upsert(make_story(platform="reddit", source_id="shared-id"))
        other = make_story(platform="fourchan", source_id="shared-id", quality_key="x")
        assert engine_for(config, stories).evaluate(other).outcome is not DedupOutcome.KNOWN


class TestLayerTwo:
    def test_the_same_url_from_a_different_source_id_is_a_duplicate(
        self, config, stories, make_story
    ) -> None:
        url = "https://www.reddit.com/r/nosleep/comments/abc/the_thing_in_the_hall/"
        original = make_story(source_id="t3_first", canonical_url=url)
        stories.upsert(original)

        repost = make_story(
            source_id="t3_second",
            canonical_url="https://old.reddit.com/r/nosleep/comments/abc/the_thing_in_the_hall?utm_source=x",
            body="A completely different body so only the URL can match.",
        )
        verdict = engine_for(
            config,
            stories,
            content_hash=False,
            near_duplicate=config.deduplication.layers.near_duplicate.model_copy(
                update={"enabled": False}
            ),
        ).evaluate(repost)
        assert verdict.outcome is DedupOutcome.DUPLICATE
        assert verdict.layer is DedupLayer.CANONICAL_URL
        assert verdict.original_id == original.id


class TestLayerThree:
    def test_identical_content_across_platforms_is_a_duplicate(
        self, config, stories, make_story
    ) -> None:
        """A Reddit post copy-pasted onto 4chan is one story."""
        original = make_story(platform="reddit", source_id="t3_orig")
        stories.upsert(original)

        crosspost = make_story(
            platform="fourchan",
            source_id="x/998877",
            canonical_url="https://boards.4chan.org/x/thread/998877",
            body=original.normalized_content,
            quality_key="x",
        )
        verdict = engine_for(config, stories).evaluate(crosspost)
        assert verdict.outcome is DedupOutcome.DUPLICATE
        assert verdict.layer is DedupLayer.CONTENT_HASH
        assert verdict.original_id == original.id

    def test_reformatted_reposts_still_collide(self, config, stories, make_story) -> None:
        original = make_story(source_id="t3_orig")
        stories.upsert(original)
        reformatted = make_story(
            source_id="t3_repost",
            canonical_url="https://www.reddit.com/r/other/comments/zzz/repost/",
            body=original.normalized_content.upper().replace("\n\n", "\n"),
        )
        verdict = engine_for(config, stories).evaluate(reformatted)
        assert verdict.outcome is DedupOutcome.DUPLICATE
        assert verdict.layer is DedupLayer.CONTENT_HASH

    def test_genuinely_different_stories_do_not_collide(self, config, stories, make_story) -> None:
        stories.upsert(make_story(source_id="t3_a"))
        other = make_story(
            source_id="t3_b",
            canonical_url="https://www.reddit.com/r/x/comments/b/",
            title="A completely unrelated matter",
            body=(
                "Quarterly freight throughput improved across the northern corridor after the "
                "depot consolidation finished. Warehouse staffing was reduced by eleven roles "
                "and the remaining shifts were rebalanced toward overnight loading. "
            )
            * 4,
        )
        assert engine_for(config, stories).evaluate(other).outcome is DedupOutcome.NEW


class TestLayerFour:
    """SimHash near-duplicate detection, and its deliberately narrow scope.

    Threshold 3 is calibrated on real content: the closest pair of genuinely
    *different* same-genre stories measures 5 bits apart. That leaves a narrow
    band, so this layer catches reposts that are substantially the same text and
    nothing more. The tests below pin both halves of that trade-off.
    """

    def test_a_reposted_story_is_caught(self, config, stories, make_story) -> None:
        """Reformatting and a word substitution are still the same story."""
        original = make_story(source_id="t3_orig")
        stories.upsert(original)

        repost = make_story(
            source_id="t3_repost",
            canonical_url="https://www.reddit.com/r/x/comments/repost/",
            body="Throwaway account.\n\n"
            + original.normalized_content.replace("roommate", "flatmate"),
        )
        assert repost.content_hash != original.content_hash

        verdict = engine_for(config, stories).evaluate(repost)
        assert verdict.outcome is DedupOutcome.DUPLICATE
        assert verdict.layer is DedupLayer.NEAR_DUPLICATE
        assert verdict.detail is not None
        assert verdict.detail["hamming_distance"] <= verdict.detail["threshold"]

    def test_a_substantially_rewritten_repost_is_not_caught(
        self, config, stories, make_story
    ) -> None:
        """The honest limit of this layer -- and why that is the right default.

        A retelling with a rewritten opening lands beyond threshold 3. Catching
        it would mean raising the threshold past the point where genuinely
        different same-genre stories start merging, which costs real stories.
        Preferring a missed duplicate over a lost story is deliberate: the
        novelty ranking signal demotes near-similar stories anyway, so a repost
        that slips through is ranked down rather than published twice.
        """
        original = make_story(source_id="t3_orig")
        stories.upsert(original)

        rewritten = make_story(
            source_id="t3_rewritten",
            canonical_url="https://www.reddit.com/r/x/comments/rewritten/",
            body="I have been covering my flatmate's rent since April and I am done.\n\n"
            + original.normalized_content.split(". ", 1)[1],
        )
        assert engine_for(config, stories).evaluate(rewritten).outcome is DedupOutcome.NEW

    def test_unrelated_stories_survive_the_near_duplicate_layer(
        self, config, stories, make_story
    ) -> None:
        stories.upsert(make_story(source_id="t3_a"))
        unrelated = make_story(
            source_id="t3_b",
            canonical_url="https://www.reddit.com/r/x/comments/b/",
            title="Depot consolidation results",
            body=(
                "Quarterly freight throughput improved across the northern corridor after the "
                "depot consolidation finished. Warehouse staffing changed and shifts were "
                "rebalanced toward overnight loading windows. "
            )
            * 6,
        )
        assert engine_for(config, stories).evaluate(unrelated).outcome is DedupOutcome.NEW

    def test_short_bodies_are_not_fingerprinted(self, config, stories, make_story) -> None:
        """Below the token floor a SimHash is unstable, so no verdict is better."""
        story = make_story(body="Way too short to fingerprint.")
        assert story.simhash is None
        assert engine_for(config, stories).evaluate(story).outcome is DedupOutcome.NEW

    def test_the_layer_can_be_disabled(self, config, stories, make_story) -> None:
        original = make_story(source_id="t3_orig")
        stories.upsert(original)
        edited = make_story(
            source_id="t3_edited",
            canonical_url="https://www.reddit.com/r/x/comments/edited/",
            body=original.normalized_content.replace("roommate", "flatmate"),
        )
        disabled = config.deduplication.layers.near_duplicate.model_copy(update={"enabled": False})
        assert (
            engine_for(config, stories, near_duplicate=disabled).evaluate(edited).outcome
            is DedupOutcome.NEW
        )


class TestEngineBehaviour:
    def test_layers_run_cheapest_first(self, config, stories) -> None:
        assert engine_for(config, stories).layers == (
            "exact_source",
            "canonical_url",
            "content_hash",
            "near_duplicate",
        )

    def test_an_already_marked_duplicate_never_matches_a_later_story(
        self, config, stories, make_story
    ) -> None:
        """Duplicates must not become the 'original' for the next arrival."""
        original = make_story(source_id="t3_orig")
        duplicate = make_story(
            source_id="t3_dupe", canonical_url="https://www.reddit.com/r/x/comments/dupe/"
        )
        stories.upsert(original)
        stories.upsert(duplicate)
        stories.transition(duplicate.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE)
        stories.mark_duplicate(
            duplicate.id, duplicate_of_id=original.id, layer=DedupLayer.CONTENT_HASH
        )

        third = make_story(
            source_id="t3_third", canonical_url="https://www.reddit.com/r/x/comments/third/"
        )
        verdict = engine_for(config, stories).evaluate(third)
        assert verdict.original_id == original.id

    def test_re_evaluating_a_stored_story_does_not_flag_it_against_itself(
        self, config, stories, make_story
    ) -> None:
        story = make_story()
        stories.upsert(story)
        stories.transition(story.id, StoryStatus.NORMALIZED, stage=PipelineStage.NORMALIZE)
        stories.transition(story.id, StoryStatus.DEDUPLICATED, stage=PipelineStage.DEDUPLICATE)

        # Skip layer 1 so layers 2-4 are forced to consider the stored row.
        engine = engine_for(config, stories, exact_source=False)
        assert engine.evaluate(story).outcome is DedupOutcome.NEW

    def test_deduplication_is_deterministic(self, config, stories, make_story) -> None:
        stories.upsert(make_story(source_id="t3_orig"))
        candidate = make_story(
            source_id="t3_new", canonical_url="https://www.reddit.com/r/x/comments/new/"
        )
        engine = engine_for(config, stories)
        first, second = engine.evaluate(candidate), engine.evaluate(candidate)
        assert first.outcome == second.outcome
        assert first.original_id == second.original_id
        assert first.layer == second.layer

    def test_the_earliest_discovery_wins_as_the_original(
        self, config, stories, make_story, clock
    ) -> None:
        first = make_story(source_id="t3_first")
        stories.upsert(first)
        clock.advance(3600)
        second = make_story(
            source_id="t3_second", canonical_url="https://www.reddit.com/r/x/comments/second/"
        )
        stories.upsert(second)

        clock.advance(3600)
        third = make_story(
            source_id="t3_third", canonical_url="https://www.reddit.com/r/x/comments/third/"
        )
        assert engine_for(config, stories).evaluate(third).original_id == first.id

    @pytest.mark.parametrize("layer", ["exact_source", "canonical_url", "content_hash"])
    def test_each_layer_can_be_turned_off_independently(
        self, config, stories, make_story, layer: str
    ) -> None:
        story = make_story()
        stories.upsert(story)
        engine = engine_for(config, stories, **{layer: False})
        assert engine.evaluate(story) is not None  # no crash, whatever the verdict
        assert layer not in engine.layers
