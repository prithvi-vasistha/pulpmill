"""Narrative suitability signal.

This is a heuristic, and it is worth being blunt about what it is not: it does
not predict virality, and it does not judge whether a story is *good*. It
measures whether a body of text has the surface features of something that
narrates well -- a first-person voice, a sequence of events, dialogue, conflict,
paragraph structure -- and penalises text that plainly does not (link dumps,
all-caps, meta posts about the subreddit itself).

Every cue is transparent, bounded by its configured weight, and reported in the
explanation, so a surprising score can always be traced to the cues that caused
it. When a smarter model eventually scores narrative quality, it slots in
alongside this as another signal rather than replacing the ranking engine.
"""

from __future__ import annotations

import re

from pulpmill.domain.ranking import SignalScore
from pulpmill.normalization.text import count_urls, paragraphs, tokenize
from pulpmill.ranking.signals.base import ScoringContext, clamp

_FIRST_PERSON = frozenset(
    {"i", "i'm", "i've", "i'd", "i'll", "me", "my", "mine", "myself", "we", "our"}
)

#: Words that indicate a story has stakes. Broad by design -- this is a coarse
#: filter, and a missing cue costs a fraction of one weight, not the ranking.
_CONFLICT_TERMS = frozenset(
    {
        "angry",
        "argue",
        "argued",
        "argument",
        "betray",
        "betrayed",
        "broke",
        "cheated",
        "confront",
        "confronted",
        "confession",
        "cried",
        "creepy",
        "divorce",
        "fight",
        "fired",
        "furious",
        "hate",
        "hid",
        "lied",
        "lying",
        "quit",
        "refused",
        "revenge",
        "ruined",
        "scared",
        "scream",
        "screamed",
        "secret",
        "shouted",
        "sobbing",
        "stole",
        "terrified",
        "threatened",
        "upset",
        "wrong",
        "yelled",
    }
)

#: Words that mark a sequence of events rather than a static description.
_TEMPORAL_TERMS = frozenset(
    {
        "after",
        "afterwards",
        "ago",
        "before",
        "eventually",
        "finally",
        "later",
        "meanwhile",
        "next",
        "afterward",
        "since",
        "soon",
        "suddenly",
        "then",
        "today",
        "tonight",
        "until",
        "when",
        "while",
        "yesterday",
    }
)

_DIALOGUE_PATTERN = re.compile(r"[\"“”]|\b(?:said|asked|replied|told me|shouted)\b", re.IGNORECASE)

#: Openings and phrases that mark a post as being about the community rather
#: than a story.
_META_PATTERN = re.compile(
    r"\b(?:mod(?:s|erator)?\b.{0,20}\b(?:please|remove|approve)|"
    r"first time posting|long time lurker|this is my first post|"
    r"repost(?:ing)? (?:because|from)|deleted (?:my|the) (?:last|previous) post|"
    r"sorry for (?:the )?(?:bad )?(?:format|english)|throwaway account because)\b",
    re.IGNORECASE,
)

_TITLE_HOOK_PREFIXES = ("aita", "wibta", "tifu", "update", "my ", "i ", "he ", "she ", "we ")

_CAPS_WORD = re.compile(r"\b[A-Z]{3,}\b")


class NarrativeSuitabilitySignal:
    """Lexical cues that a body of text will narrate well."""

    name = "narrative_suitability"

    def score(self, context: ScoringContext) -> SignalScore:
        config = context.config.ranking.narrative_suitability
        story = context.story
        body = story.normalized_content
        tokens = tokenize(body)
        total = len(tokens)

        if total == 0:
            return SignalScore(
                name=self.name, value=0.0, detail={"reason": "empty normalized content"}
            )

        token_set = set(tokens)

        # --- positive cues, each normalized to [0, 1] ---
        first_person_hits = sum(1 for token in tokens if token in _FIRST_PERSON)
        # ~4% first-person tokens is a solidly first-person narrative.
        first_person = clamp((first_person_hits / total) / 0.04)

        dialogue_hits = len(_DIALOGUE_PATTERN.findall(body))
        dialogue = clamp(dialogue_hits / 6.0)

        conflict_hits = len(token_set & _CONFLICT_TERMS)
        conflict = clamp(conflict_hits / 5.0)

        temporal_hits = len(token_set & _TEMPORAL_TERMS)
        temporal = clamp(temporal_hits / 5.0)

        block_count = len(paragraphs(body))
        structure = clamp(block_count / 5.0)

        title_hook = self._title_hook(story.title)

        positives = (
            first_person * config.first_person_weight
            + dialogue * config.dialogue_weight
            + conflict * config.conflict_weight
            + temporal * config.temporal_structure_weight
            + structure * config.paragraph_structure_weight
            + title_hook * config.title_hook_weight
        )

        # --- penalties ---
        url_count = count_urls(body)
        # One incidental link is fine; a link every 100 words is a link dump.
        link_ratio = clamp((url_count / max(total, 1)) / 0.01)
        caps_hits = len(_CAPS_WORD.findall(body))
        shouting = clamp((caps_hits / total) / 0.03)
        meta = 1.0 if _META_PATTERN.search(body) else 0.0

        penalties = (
            link_ratio * config.link_heavy_penalty
            + shouting * config.shouting_penalty
            + meta * config.meta_post_penalty
        )

        value = clamp(positives - penalties)

        return SignalScore(
            name=self.name,
            value=value,
            detail={
                "cues": {
                    "first_person": round(first_person, 4),
                    "dialogue": round(dialogue, 4),
                    "conflict": round(conflict, 4),
                    "temporal_structure": round(temporal, 4),
                    "paragraph_structure": round(structure, 4),
                    "title_hook": round(title_hook, 4),
                },
                "penalties": {
                    "link_heavy": round(link_ratio, 4),
                    "shouting": round(shouting, 4),
                    "meta_post": meta,
                },
                "evidence": {
                    "tokens": total,
                    "first_person_tokens": first_person_hits,
                    "dialogue_markers": dialogue_hits,
                    "conflict_terms": conflict_hits,
                    "temporal_terms": temporal_hits,
                    "paragraphs": block_count,
                    "urls": url_count,
                    "shouted_words": caps_hits,
                },
                "positive_subtotal": round(positives, 4),
                "penalty_subtotal": round(penalties, 4),
            },
        )

    @staticmethod
    def _title_hook(title: str) -> float:
        """Whether the title promises a story rather than describing a topic."""
        lowered = title.strip().lower()
        if not lowered:
            return 0.0
        score = 0.0
        if lowered.startswith(_TITLE_HOOK_PREFIXES):
            score += 0.5
        if "?" in title:
            score += 0.25
        # Too short to set up a story, too long to read as a hook.
        if 25 <= len(title) <= 120:
            score += 0.25
        return clamp(score)


class SourceQualitySignal:
    """Operator-assigned trust in the community the story came from.

    Reads `sources.<platform>.quality`, overridden per community by
    `quality_overrides` keyed on the `quality_key` metadata each adapter sets.
    The signal never learns what a subreddit or a board is -- it just looks up a
    key -- which is what keeps ranking decoupled from any particular source.
    """

    name = "source_quality"

    def score(self, context: ScoringContext) -> SignalScore:
        story = context.story
        quality_key = story.metadata.get("quality_key")
        quality_key = str(quality_key) if quality_key else None
        value = context.config.source_quality(story.source_platform, quality_key)
        source = context.config.source(story.source_platform)

        return SignalScore(
            name=self.name,
            value=clamp(value),
            detail={
                "platform": story.source_platform,
                "quality_key": quality_key,
                "base_quality": source.quality if source else None,
                "override_applied": bool(source and quality_key in source.quality_overrides),
            },
        )
