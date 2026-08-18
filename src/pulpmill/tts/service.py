"""The narration stage: a script becomes one audio track with word timings.

Per-line synthesis, then concatenation. That order is what makes the timings
trustworthy: every sentence boundary is a *measured* clip length rather than an
estimate, so captions cannot drift over the length of a video the way they do
when one long clip is subdivided arithmetically.

Two levels of caching, both keyed on content:

* **Per line.** Provider-owned. A story re-scripted with a different hook
  re-synthesises the hook and reuses every body line.
* **Per track.** Owned here. An unchanged script resolves to the file already on
  disk without touching the model at all.

At 200 videos a week, that is the difference between minutes and hours of
compute, and it is why `SpeechRequest.cache_key` covers voice, speed and model
version rather than text alone.
"""

from __future__ import annotations

import hashlib
import wave
from dataclasses import dataclass
from pathlib import Path

from pulpmill.config.models import AppConfig, TTSConfig
from pulpmill.domain.errors import SynthesisError, TTSUnavailableError
from pulpmill.domain.media import AudioArtifact, WordTiming, build_audio_id
from pulpmill.domain.script import NarrationScript
from pulpmill.infrastructure.logging import get_logger
from pulpmill.tts import MockTTSProvider, SpeechRequest, TTSProvider
from pulpmill.tts.alignment import distribute_words
from pulpmill.tts.kokoro import KokoroProvider

_log = get_logger("tts.service")


@dataclass(frozen=True, slots=True)
class SynthesisResult:
    artifact: AudioArtifact
    cache_key: str
    #: True when the whole track came from cache and the model was never loaded.
    cached: bool
    #: Lines that came from the per-line cache. Useful for reporting how much
    #: work a re-run actually avoided.
    lines_cached: int
    lines_total: int


def build_tts_provider(config: AppConfig, *, name: str | None = None) -> TTSProvider:
    """Instantiate the configured provider. Never raises for a missing model."""
    provider_name = name or config.tts.provider
    if provider_name == "mock":
        return MockTTSProvider(sample_rate=config.tts.sample_rate)
    kokoro = config.tts.kokoro
    return KokoroProvider(
        repo_id=kokoro.repo_id,
        model_path=str(config.resolve(kokoro.model_path)) if kokoro.model_path else "",
        voices_path=str(config.resolve(kokoro.voices_path)) if kokoro.voices_path else "",
        device=kokoro.device,
        language=config.tts.language,
        max_clip_seconds=config.tts.max_clip_seconds,
    )


class NarrationSynthesizer:
    """Turns scripts into audio tracks under one configuration."""

    def __init__(
        self,
        *,
        config: TTSConfig,
        provider: TTSProvider,
        cache_dir: Path,
    ) -> None:
        self._config = config
        self._provider = provider
        self._cache_dir = cache_dir
        self._clip_dir = cache_dir / "clips"
        self._track_dir = cache_dir / "tracks"

    @property
    def provider_name(self) -> str:
        return self._provider.name

    def available(self) -> tuple[bool, str]:
        return self._provider.available()

    def synthesize(self, script: NarrationScript, *, force: bool = False) -> SynthesisResult:
        """Produce the complete narration track for one script."""
        available, detail = self._provider.available()
        if not available:
            raise TTSUnavailableError(detail, provider=self._provider.name, script_id=script.id)

        cache_key = self._track_key(script)
        self._track_dir.mkdir(parents=True, exist_ok=True)
        track_path = self._track_dir / f"{cache_key}.wav"

        clips: list[tuple[Path, float, str]] = []
        gaps: list[float] = []
        lines_cached = 0

        for position, line in enumerate(script.lines):
            result = self._provider.synthesize(
                SpeechRequest(
                    text=line.speech_text,
                    voice_id=self._config.voice,
                    speed=self._config.speed,
                    language=self._config.language,
                    metadata={"script_id": script.id, "line": line.index},
                ),
                output_dir=self._clip_dir,
            )
            if result.cached:
                lines_cached += 1
            clips.append((result.audio_path, result.duration_seconds, line.speech_text))

            is_last = position == len(script.lines) - 1
            next_line = None if is_last else script.lines[position + 1]
            gaps.append(
                0.0
                if is_last
                else (
                    self._config.paragraph_gap_seconds
                    if next_line is not None and next_line.paragraph_break
                    else self._config.sentence_gap_seconds
                )
            )

        reused_track = track_path.is_file() and not force
        if reused_track:
            duration = _wav_duration(track_path)
        else:
            duration = _concatenate(
                [path for path, _, _ in clips],
                gaps=gaps,
                destination=track_path,
                sample_rate=self._config.sample_rate,
            )

        timings = _timeline([(text, seconds) for _, seconds, text in clips], gaps)

        _log.info(
            "narration_synthesized",
            script_id=script.id,
            story_id=script.story_id,
            provider=self._provider.name,
            voice=self._config.voice,
            duration_seconds=round(duration, 2),
            lines=len(script.lines),
            lines_cached=lines_cached,
            track_cached=reused_track,
        )

        artifact = AudioArtifact(
            id=build_audio_id(script.id),
            script_id=script.id,
            story_id=script.story_id,
            path=track_path,
            duration_seconds=duration,
            sample_rate=self._config.sample_rate,
            voice_id=self._config.voice,
            provider=self._provider.name,
            model_version=self._provider.model_version,
            provenance=script.provenance,
            word_timings=timings,
            metadata={
                "lines": len(script.lines),
                "lines_cached": lines_cached,
                "speed": self._config.speed,
                "part_number": script.part_number,
                "total_parts": script.total_parts,
            },
        )
        return SynthesisResult(
            artifact=artifact,
            cache_key=cache_key,
            cached=reused_track,
            lines_cached=lines_cached,
            lines_total=len(script.lines),
        )

    def _track_key(self, script: NarrationScript) -> str:
        """Content hash covering everything that changes the finished track."""
        digest = hashlib.sha256()
        for part in (
            script.speech_text,
            self._config.voice,
            f"{self._config.speed:.3f}",
            self._config.language,
            f"{self._config.sentence_gap_seconds:.3f}",
            f"{self._config.paragraph_gap_seconds:.3f}",
            self._provider.model_version,
        ):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x1f")
        return digest.hexdigest()[:32]


def _timeline(segments: list[tuple[str, float]], gaps: list[float]) -> tuple[WordTiming, ...]:
    """Lay per-line word timings onto the concatenated timeline."""
    timings: list[WordTiming] = []
    offset = 0.0
    for index, (text, duration) in enumerate(segments):
        if duration > 0:
            timings.extend(distribute_words(text, start=offset, end=offset + duration))
        offset += duration + gaps[index]
    return tuple(timings)


def _wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())


def _concatenate(
    clips: list[Path], *, gaps: list[float], destination: Path, sample_rate: int
) -> float:
    """Join mono WAV clips with silence between them.

    Done with the standard library rather than by shelling out to ffmpeg: the
    inputs are already uniform PCM, so a concat filter would add a process spawn
    and a temp file per video for no benefit. Frame counts are also exact this
    way, which is what the timing model depends on.
    """
    if not clips:
        raise SynthesisError("cannot build a track with no clips")

    total_frames = 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(destination), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(sample_rate)

        for index, clip in enumerate(clips):
            with wave.open(str(clip), "rb") as source:
                if source.getframerate() != sample_rate or source.getnchannels() != 1:
                    raise SynthesisError(
                        "clip format does not match the track",
                        clip=clip.name,
                        expected_rate=sample_rate,
                        actual_rate=source.getframerate(),
                        channels=source.getnchannels(),
                    )
                frames = source.readframes(source.getnframes())
            output.writeframes(frames)
            total_frames += len(frames) // 2

            silence_frames = int(gaps[index] * sample_rate)
            if silence_frames > 0:
                output.writeframes(b"\x00\x00" * silence_frames)
                total_frames += silence_frames

    return total_frames / float(sample_rate)
