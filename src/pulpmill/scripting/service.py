"""The script stage: a story becomes one or more narration scripts.

The division of labour here is the whole point of the module:

* **Segmentation and numbering are computed.** `plan_segments` proposes ranges,
  `domain.series.plan_parts` turns them into numbered parts. Neither consults a
  model, and a provider cannot reach either.
* **Phrasing is advisory.** A provider may improve the hook and suggest better
  cut points. Its cut points are validated against the actual sentence list and
  discarded whole if anything is out of range.
* **Failure is never silent.** A provider that is unavailable, times out, or
  returns unusable advice produces a deterministic script and a recorded
  `fallback_reason`, exactly as the editorial stage does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from pulpmill.config.models import AppConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.domain.errors import ScriptError
from pulpmill.domain.script import (
    LineRole,
    NarrationScript,
    ScriptLine,
    build_script_id,
)
from pulpmill.domain.series import StoryPart, plan_parts
from pulpmill.domain.story import Story
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock
from pulpmill.infrastructure.logging import get_logger
from pulpmill.ingestion.base import QUALITY_KEY
from pulpmill.scripting.claude import ClaudeScriptProvider
from pulpmill.scripting.deterministic import DeterministicScriptProvider
from pulpmill.scripting.hooks import build_hook, build_outro, tidy_title
from pulpmill.scripting.provider import ScriptBrief, ScriptGuidance, ScriptProvider
from pulpmill.scripting.segmentation import (
    SegmentPlan,
    Sentence,
    plan_segments,
    speech_durations,
    split_sentences,
    subdivide_long_sentences,
)
from pulpmill.scripting.speech import to_speech_text

_log = get_logger("scripting.service")


@dataclass(frozen=True, slots=True)
class ScriptResult:
    """Everything the script stage produced for one story."""

    scripts: tuple[NarrationScript, ...]
    parts: tuple[StoryPart, ...]
    series_id: str
    provider: str
    effective_provider: str
    fallback_reason: str | None
    notes: str

    @property
    def used_fallback(self) -> bool:
        return self.provider != self.effective_provider

    @property
    def total_parts(self) -> int:
        return len(self.scripts)


def build_script_provider(
    config: AppConfig, secrets: SecretStore, *, name: str | None = None
) -> ScriptProvider:
    """Instantiate the configured provider. Never raises for a missing key."""
    provider_name = name or config.script.provider
    if provider_name == "claude":
        return ClaudeScriptProvider(
            config.script.claude,
            api_key=secrets.get("ANTHROPIC_API_KEY", prefixed=False),
        )
    return DeterministicScriptProvider()


class ScriptBuilder:
    """Turns stories into scripts under one configuration."""

    def __init__(
        self,
        *,
        config: AppConfig,
        provider: ScriptProvider | None = None,
        clock: Clock = SYSTEM_CLOCK,
    ) -> None:
        self._config = config
        self._script_config = config.script
        self._provider = provider or DeterministicScriptProvider()
        self._fallback = DeterministicScriptProvider()
        self._clock = clock

    def build(self, story: Story) -> ScriptResult:
        """Produce every script for one story.

        Raises `ScriptError` when the story cannot be scripted at all, and
        `StoryTooLongError` when it would need more parts than allowed. Neither
        is a bug: both mean this story is not publishable as configured.
        """
        config = self._script_config
        sentences, speech_texts = _speakable_sentences(
            story.normalized_content, max_words_per_chunk=self._config.tts.max_words_per_chunk
        )
        if not sentences:
            raise ScriptError("story has no narratable sentences", story_id=story.id)

        # Durations are measured on the spoken form throughout. Planning against
        # the written form underestimates by up to 40% on this corpus.
        durations = speech_durations(speech_texts, words_per_minute=config.words_per_minute)
        brief = self._build_brief(story, sentences, durations)

        if brief.estimated_seconds < config.min_seconds:
            raise ScriptError(
                "story is too short to narrate",
                story_id=story.id,
                estimated_seconds=round(brief.estimated_seconds, 1),
                minimum=config.min_seconds,
            )

        guidance, effective_provider, fallback_reason = self._guidance(brief)
        plan = self._plan(sentences, durations, guidance, story_id=story.id)
        series_id, parts = plan_parts(
            story_id=story.id,
            provenance=story.provenance,
            boundaries=plan.boundaries,
            content_length=len(story.normalized_content),
        )
        if len(parts) != plan.total_parts:  # pragma: no cover - guards a planning bug
            raise ScriptError(
                "part planning disagreed with segmentation",
                story_id=story.id,
                planned=plan.total_parts,
                parts=len(parts),
            )

        # Two different limits, deliberately. The title card has to fit on
        # screen; the hook is narrated, not drawn, so it keeps the full title.
        # Building the hook from the card text truncated it mid-phrase and
        # dropped the last few words, which are usually the interesting ones.
        source_title = guidance.title or story.title
        card_title = tidy_title(source_title)
        spoken_title = tidy_title(source_title, max_chars=len(source_title) + 1)

        scripts = tuple(
            self._build_part_script(
                story,
                sentences=sentences[start:stop],
                speech_texts=speech_texts[start:stop],
                part=parts[index],
                title=card_title,
                spoken_title=spoken_title,
                hook_override=guidance.hook if index == 0 else None,
                estimated_seconds=plan.estimated_seconds[index],
                generator=effective_provider,
            )
            for index, (start, stop) in enumerate(plan.ranges)
        )

        _log.info(
            "script_built",
            story_id=story.id,
            parts=len(scripts),
            provider=self._provider.name,
            effective_provider=effective_provider,
            estimated_seconds=[round(value, 1) for value in plan.estimated_seconds],
        )

        return ScriptResult(
            scripts=scripts,
            parts=parts,
            series_id=series_id,
            provider=self._provider.name,
            effective_provider=effective_provider,
            fallback_reason=fallback_reason,
            notes=guidance.notes,
        )

    # --- internals -----------------------------------------------------------

    def _build_brief(
        self, story: Story, sentences: Sequence[Sentence], durations: Sequence[float]
    ) -> ScriptBrief:
        config = self._script_config
        community = str(story.metadata.get(QUALITY_KEY) or story.source_platform)
        return ScriptBrief(
            story_id=story.id,
            title=story.title,
            source_platform=story.source_platform,
            community=community,
            sentences=tuple(sentence.text for sentence in sentences),
            word_count=sum(sentence.word_count for sentence in sentences),
            estimated_seconds=sum(durations),
            target_seconds=config.target_seconds,
            max_seconds=config.max_seconds,
            max_parts=config.max_parts,
        )

    def _guidance(self, brief: ScriptBrief) -> tuple[ScriptGuidance, str, str | None]:
        """Ask the configured provider, falling back on any failure."""
        available, detail = self._provider.available()
        if not available:
            if self._provider.name != self._fallback.name:
                _log.warning(
                    "script_provider_unavailable",
                    provider=self._provider.name,
                    reason=detail,
                    falling_back_to=self._fallback.name,
                )
            return self._fallback.guide(brief), self._fallback.name, detail

        try:
            return self._provider.guide(brief), self._provider.name, None
        except ScriptError as exc:
            _log.warning(
                "script_provider_failed",
                provider=self._provider.name,
                story_id=brief.story_id,
                error=str(exc),
                error_type=type(exc).__name__,
                falling_back_to=self._fallback.name,
            )
            return self._fallback.guide(brief), self._fallback.name, str(exc)

    def _plan(
        self,
        sentences: list[Sentence],
        durations: Sequence[float],
        guidance: ScriptGuidance,
        *,
        story_id: str,
    ) -> SegmentPlan:
        """Honour validated cut points, or compute an even split."""
        config = self._script_config
        if guidance.cut_after:
            plan = _plan_from_cuts(sentences, durations, guidance.cut_after)
            longest = max(plan.estimated_seconds)
            if longest <= config.max_seconds:
                return plan
            _log.warning(
                "script_cut_points_rejected",
                story_id=story_id,
                reason="a proposed part exceeds max_seconds",
                longest_seconds=round(longest, 1),
                max_seconds=config.max_seconds,
            )

        return plan_segments(
            sentences,
            durations=durations,
            target_seconds=config.target_seconds,
            max_seconds=config.max_seconds,
            max_parts=config.max_parts,
        )

    def _build_part_script(
        self,
        story: Story,
        *,
        sentences: Sequence[Sentence],
        speech_texts: Sequence[str],
        part: StoryPart,
        title: str,
        spoken_title: str,
        hook_override: str | None,
        estimated_seconds: float,
        generator: str,
    ) -> NarrationScript:
        config = self._script_config
        lines: list[ScriptLine] = []

        if config.include_hook:
            hook_text = hook_override or build_hook(
                title=spoken_title,
                first_sentence=sentences[0].text if sentences else "",
                part_number=part.part_number,
                total_parts=part.total_parts,
            )
            hook_line = _make_line(len(lines), LineRole.HOOK, hook_text)
            if hook_line is not None:
                lines.append(hook_line)

        for sentence, speech in zip(sentences, speech_texts, strict=True):
            lines.append(
                ScriptLine(
                    index=len(lines),
                    role=LineRole.BODY,
                    text=sentence.text,
                    speech_text=speech,
                    paragraph_break=sentence.paragraph_break,
                )
            )

        if not any(line.role is LineRole.BODY for line in lines):
            raise ScriptError(
                "part contains no speakable text",
                story_id=story.id,
                part_number=part.part_number,
            )

        if config.include_outro:
            outro = build_outro(
                part_number=part.part_number,
                total_parts=part.total_parts,
                template=config.outro_template,
                final_outro=config.final_outro,
            )
            outro_line = (
                _make_line(len(lines), LineRole.OUTRO, outro, break_before=True) if outro else None
            )
            if outro_line is not None:
                lines.append(outro_line)

        return NarrationScript(
            id=build_script_id(story.id, part.part_number),
            story_id=story.id,
            part_number=part.part_number,
            total_parts=part.total_parts,
            series_id=part.series_id,
            part_id=part.id,
            provenance=story.provenance,
            title=title,
            lines=tuple(lines),
            generator=generator,
            generator_version=config.version,
            config_fingerprint=self._config.production_fingerprint(),
            created_at=self._clock.now(),
            metadata={
                "community": str(story.metadata.get(QUALITY_KEY) or story.source_platform),
                "estimated_seconds": round(estimated_seconds, 2),
                "source_word_count": story.word_count,
            },
        )


def _spoken_word_count(text: str) -> int:
    """Length of a fragment as the synthesiser will see it."""
    return len(to_speech_text(text).split())


def _speakable_sentences(
    content: str, *, max_words_per_chunk: int
) -> tuple[list[Sentence], list[str]]:
    """Split into narratable sentences, paired with their spoken form.

    Two things happen here, both before anything is planned, so that sentence
    indices, durations and script lines stay aligned for the rest of the stage:

    * **Over-long sentences are subdivided.** A run-on post exceeds the
      synthesiser's token ceiling and cannot be split across parts, so it would
      otherwise force a part past `max_seconds`.
    * **Unspeakable sentences are dropped.** A sentence that is only
      punctuation, an emoji or a bare quote marker survives text normalisation
      but has nothing to narrate.
    """
    split = subdivide_long_sentences(
        split_sentences(content),
        max_words=max_words_per_chunk,
        measure=_spoken_word_count,
    )

    sentences: list[Sentence] = []
    speech_texts: list[str] = []
    for sentence in split:
        speech = to_speech_text(sentence.text)
        if not speech.strip():
            continue
        sentences.append(sentence)
        speech_texts.append(speech)
    return sentences, speech_texts


def _make_line(
    index: int, role: LineRole, text: str, *, break_before: bool = False
) -> ScriptLine | None:
    """Build a pipeline-authored line, or None when it has nothing to say."""
    display = text.strip()
    speech = to_speech_text(display)
    if not speech.strip():
        return None
    return ScriptLine(
        index=index,
        role=role,
        text=display,
        speech_text=speech,
        paragraph_break=break_before,
    )


def _plan_from_cuts(
    sentences: Sequence[Sentence], durations: Sequence[float], cuts: Sequence[int]
) -> SegmentPlan:
    """Turn validated cut-after indices into a segment plan."""
    ranges: list[tuple[int, int]] = []
    previous = 0
    for cut in cuts:
        ranges.append((previous, cut + 1))
        previous = cut + 1
    ranges.append((previous, len(sentences)))
    return SegmentPlan(
        ranges=tuple(ranges),
        boundaries=tuple(sentences[start].start for start, _ in ranges[1:]),
        estimated_seconds=tuple(sum(durations[start:stop]) for start, stop in ranges),
    )
