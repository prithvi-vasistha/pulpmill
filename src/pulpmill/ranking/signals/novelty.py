"""Novelty signal."""

from __future__ import annotations

from pulpmill.domain.ranking import SignalScore
from pulpmill.normalization.text import jaccard, shingles, tokenize
from pulpmill.ranking.signals.base import ScoringContext


class NoveltySignal:
    """How unlike the recently-discovered corpus this story is.

    Deduplication removes stories that *are* the same. This signal demotes
    stories that are merely similar -- the fourth "my roommate stopped paying
    rent" of the week -- which is what stops a channel producing the same video
    repeatedly.

    Comparison is n-gram Jaccard over the title plus a bounded prefix of the
    body. Bounded on purpose: the corpus is held in memory on a machine that
    will also be rendering video.
    """

    name = "novelty"

    def score(self, context: ScoringContext) -> SignalScore:
        config = context.config.ranking.novelty
        story = context.story

        subject = f"{story.title}\n{story.normalized_content[: config.compare_chars]}"
        tokens = tokenize(subject)

        if len(tokens) < config.min_tokens:
            return SignalScore(
                name=self.name,
                value=0.0,
                available=False,
                detail={
                    "reason": "too few tokens to compare",
                    "tokens": len(tokens),
                    "min_tokens": config.min_tokens,
                },
            )

        if not context.novelty_corpus:
            return SignalScore(
                name=self.name,
                value=1.0,
                detail={"corpus_size": 0, "reason": "nothing to compare against yet"},
            )

        own = shingles(tokens, config.shingle_size)
        best_similarity = 0.0
        nearest_id: str | None = None

        for entry in context.novelty_corpus:
            if entry.story_id == story.id:
                continue
            other = shingles(
                tokenize(f"{entry.title}\n{entry.content_prefix}"), config.shingle_size
            )
            if not other:
                continue
            similarity = jaccard(own, other)
            if similarity > best_similarity:
                best_similarity = similarity
                nearest_id = entry.story_id

        return SignalScore(
            name=self.name,
            value=max(0.0, min(1.0, 1.0 - best_similarity)),
            detail={
                "corpus_size": len(context.novelty_corpus),
                "max_similarity": round(best_similarity, 4),
                "nearest_story_id": nearest_id,
                "shingle_size": config.shingle_size,
                "compare_chars": config.compare_chars,
            },
        )
