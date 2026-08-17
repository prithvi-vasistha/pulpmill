"""Engagement and comment-activity signals."""

from __future__ import annotations

from pulpmill.domain.ranking import SignalScore
from pulpmill.ranking.signals.base import ScoringContext, saturating


class EngagementSignal:
    """How much attention the story attracted, normalized per platform.

    Reddit upvotes and 4chan replies are incomparable units, so each axis is
    normalized against a per-source reference from
    `sources.<name>.engagement`. A story sitting exactly at the reference
    scores 0.5 on that axis.

    An axis the platform does not report (4chan has no score) is *dropped*, not
    scored zero. If a platform reports nothing at all the whole signal reports
    itself unavailable and the engine redistributes its weight -- otherwise
    every 4chan thread would be permanently penalised for a metric that does
    not exist there.
    """

    name = "engagement"

    def score(self, context: ScoringContext) -> SignalScore:
        story = context.story
        references = context.config.engagement_references(story.source_platform)
        engagement = story.engagement

        axes: dict[str, float] = {}
        detail: dict[str, object] = {"platform": story.source_platform}

        if references.score_reference is not None and engagement.score is not None:
            axes["score"] = saturating(float(engagement.score), references.score_reference)
            detail["score"] = engagement.score
            detail["score_reference"] = references.score_reference

        if references.comment_reference is not None and engagement.comments is not None:
            axes["comments"] = saturating(float(engagement.comments), references.comment_reference)
            detail["comments"] = engagement.comments
            detail["comment_reference"] = references.comment_reference

        if not axes:
            return SignalScore(
                name=self.name,
                value=0.0,
                available=False,
                detail={**detail, "reason": "platform reports no comparable engagement metric"},
            )

        value = sum(axes.values()) / len(axes)
        detail["axes"] = {key: round(score, 4) for key, score in axes.items()}
        return SignalScore(name=self.name, value=value, detail=detail)


class CommentActivitySignal:
    """Discussion rate rather than raw discussion volume.

    A thread earning 40 comments an hour is a livelier story than one that
    accumulated 400 over a fortnight. The first `min_age_hours` is excluded
    because early comment rates are noise.
    """

    name = "comment_activity"

    def score(self, context: ScoringContext) -> SignalScore:
        config = context.config.ranking.comment_activity
        comments = context.story.engagement.comments

        if comments is None:
            return SignalScore(
                name=self.name,
                value=0.0,
                available=False,
                detail={"reason": "platform reports no comment count"},
            )

        effective_age = max(context.age_hours, config.min_age_hours)
        per_hour = comments / effective_age if effective_age > 0 else 0.0
        value = saturating(per_hour, config.reference_comments_per_hour)

        return SignalScore(
            name=self.name,
            value=value,
            detail={
                "comments": comments,
                "age_hours": round(context.age_hours, 3),
                "effective_age_hours": round(effective_age, 3),
                "comments_per_hour": round(per_hour, 3),
                "reference_comments_per_hour": config.reference_comments_per_hour,
            },
        )
