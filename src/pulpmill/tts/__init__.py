"""Text-to-speech provider interface.

Declared now, with a working mock, so the narration stage can be added without
the rest of the application learning what Kokoro is. Nothing in the pipeline
depends on a concrete provider -- `TTSProvider` is the only type anything else
will import.

Kokoro is the intended local provider (GPU-backed, offline). `KokoroProvider`
is not implemented tonight because there is no audio stage to feed yet, and a
stub pretending to synthesise audio would be worse than an honest absence.
`MockTTSProvider` below is real and testable: it produces a deterministic
`SpeechResult` with a plausible duration, which is enough to build and test the
timing, subtitle and rendering stages against.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from pulpmill.domain.media import WordTiming

#: Words per minute for duration estimation. Roughly conversational narration.
DEFAULT_WPM = 150.0


@dataclass(frozen=True, slots=True)
class SpeechRequest:
    """One synthesis request.

    `cache_key` is derived from everything that affects the audio, so a repeated
    request for identical text with an identical voice and model resolves to the
    same file instead of re-synthesising. Regenerating 200 videos a week makes
    that difference material.
    """

    text: str
    voice_id: str
    #: Playback rate multiplier, 1.0 = natural.
    speed: float = 1.0
    language: str = "en"
    #: Carried through onto the result for provenance.
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def cache_key(self, *, model_version: str) -> str:
        digest = hashlib.sha256()
        for part in (self.text, self.voice_id, f"{self.speed:.3f}", self.language, model_version):
            digest.update(part.encode("utf-8"))
            digest.update(b"\x1f")
        return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class SpeechResult:
    """A synthesised clip and everything needed to reproduce or caption it."""

    audio_path: Path
    duration_seconds: float
    voice_id: str
    model_version: str
    sample_rate: int
    #: Empty when the provider cannot supply word-level alignment; the subtitle
    #: stage falls back to even distribution in that case.
    word_timings: tuple[WordTiming, ...] = ()
    cached: bool = False
    metadata: Mapping[str, Any] = field(default_factory=dict)


@runtime_checkable
class TTSProvider(Protocol):
    @property
    def name(self) -> str: ...

    @property
    def model_version(self) -> str:
        """Identifies the synthesis model. Part of the cache key."""
        ...

    def available(self) -> tuple[bool, str]:
        """Whether synthesis can run right now, and why not if it cannot."""
        ...

    def voices(self) -> tuple[str, ...]:
        """Voice identifiers this provider accepts."""
        ...

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechResult: ...


class MockTTSProvider:
    """A real, deterministic provider that writes silence.

    Not a placeholder pretending to be Kokoro: it does exactly what it says,
    producing a valid WAV of the estimated duration plus evenly-distributed word
    timings. That is enough to develop and test subtitle timing and FFmpeg
    composition without a GPU, and to keep those stages honest about handling a
    provider that returns approximate alignment.
    """

    def __init__(self, *, wpm: float = DEFAULT_WPM, sample_rate: int = 24_000) -> None:
        self._wpm = wpm
        self._sample_rate = sample_rate

    @property
    def name(self) -> str:
        return "mock"

    @property
    def model_version(self) -> str:
        return "mock-1"

    def available(self) -> tuple[bool, str]:
        return True, "always available (writes silence)"

    def voices(self) -> tuple[str, ...]:
        return ("mock-neutral", "mock-warm")

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechResult:
        import wave

        words = request.text.split()
        duration = max(0.5, len(words) / self._wpm * 60.0 / max(request.speed, 0.1))

        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{request.cache_key(model_version=self.model_version)}.wav"

        cached = path.exists()
        if not cached:
            frames = int(duration * self._sample_rate)
            with wave.open(str(path), "wb") as handle:
                handle.setnchannels(1)
                handle.setsampwidth(2)
                handle.setframerate(self._sample_rate)
                handle.writeframes(b"\x00\x00" * frames)

        per_word = duration / len(words) if words else 0.0
        timings = tuple(
            WordTiming(
                word=word,
                start_seconds=round(index * per_word, 4),
                end_seconds=round((index + 1) * per_word, 4),
            )
            for index, word in enumerate(words)
        )

        return SpeechResult(
            audio_path=path,
            duration_seconds=round(duration, 3),
            voice_id=request.voice_id,
            model_version=self.model_version,
            sample_rate=self._sample_rate,
            word_timings=timings,
            cached=cached,
            metadata=dict(request.metadata),
        )


def estimate_duration_seconds(word_count: int, *, wpm: float = DEFAULT_WPM) -> float:
    """Narration length estimate, used for planning before any audio exists."""
    if word_count <= 0:
        return 0.0
    return word_count / wpm * 60.0


__all__ = [
    "DEFAULT_WPM",
    "MockTTSProvider",
    "SpeechRequest",
    "SpeechResult",
    "TTSProvider",
    "WordTiming",
    "estimate_duration_seconds",
]
