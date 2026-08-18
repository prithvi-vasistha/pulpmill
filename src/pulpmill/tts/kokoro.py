"""Kokoro-82M speech synthesis.

Kokoro is the intended local voice: 82M parameters, permissively licensed, fast
enough to narrate a 60-second video in a few seconds on this hardware, and
entirely offline once its weights are on disk. No API, no per-character cost, no
network dependency in the hot path.

**Two backends, one provider.** Kokoro ships in two forms and which one a
machine has installed is not something the pipeline should care about:

* `kokoro` -- the reference package, built on PyTorch. Best quality, ~2.5 GB of
  dependencies, uses CUDA when available.
* `kokoro-onnx` -- ONNX Runtime. Roughly a tenth of the install size and runs
  well on CPU, which matters on a laptop already rendering video on the GPU.

Whichever is importable is used, ONNX first because it is the cheaper one to
have installed. Neither is a hard dependency: `available()` reports what is
missing and the exact command to fix it, and the pipeline falls back to the mock
provider rather than failing.

Synthesis is per-utterance by design -- see `pulpmill.tts.alignment` for why
that is what makes caption timing exact.
"""

from __future__ import annotations

import sys
import wave
from array import array
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from pulpmill.domain.errors import SynthesisError, TTSUnavailableError
from pulpmill.infrastructure.logging import get_logger
from pulpmill.tts import SpeechRequest, SpeechResult
from pulpmill.tts.alignment import distribute_words

PROVIDER_NAME = "kokoro"

#: Kokoro's native output rate. Not configurable: resampling would cost quality
#: for nothing, since every downstream stage accepts whatever rate it is given.
KOKORO_SAMPLE_RATE = 24_000

#: Language codes the ONNX backend accepts, mapped to the single-letter codes
#: the PyTorch backend uses. Kokoro phonemises through espeak-ng, which wants a
#: region ("en-us"); bare "en" is rejected outright rather than defaulted.
LANGUAGE_CODES: dict[str, str] = {
    "en-us": "a",
    "en-gb": "b",
    "en": "a",
    "fr-fr": "f",
    "it": "i",
    "ja": "j",
    "pt-br": "p",
    "zh": "z",
}

#: Shipped voices, used when a backend cannot enumerate its own. The prefix
#: encodes accent and gender: a/b = American/British, f/m = female/male, and it
#: should agree with the configured language -- an `af_` voice reading `en-gb`
#: works, but sounds like neither accent.
KNOWN_VOICES: tuple[str, ...] = (
    "af_heart",
    "af_bella",
    "af_nicole",
    "af_sarah",
    "af_sky",
    "af_nova",
    "af_aoede",
    "af_kore",
    "am_adam",
    "am_michael",
    "am_fenrir",
    "am_puck",
    "am_onyx",
    "bf_emma",
    "bf_isabella",
    "bf_alice",
    "bf_lily",
    "bm_george",
    "bm_lewis",
    "bm_daniel",
    "bm_fable",
)

_INSTALL_HINT = "install one of: `uv sync --extra tts` (ONNX, small) or `uv pip install kokoro`"

_log = get_logger("tts.kokoro")


class _Backend(Protocol):
    """What both Kokoro packages have in common, once wrapped."""

    @property
    def name(self) -> str: ...

    @property
    def model_version(self) -> str: ...

    def voices(self) -> tuple[str, ...]: ...

    def synthesize(self, text: str, *, voice: str, speed: float, language: str) -> list[float]: ...


class _OnnxBackend:
    """kokoro-onnx. Small install, good CPU performance."""

    def __init__(self, *, model_path: str, voices_path: str) -> None:
        from kokoro_onnx import Kokoro

        # kokoro-onnx does not ship weights and does not fetch them. Being
        # explicit about that here produces one actionable error instead of a
        # FileNotFoundError from inside the package.
        if not model_path or not voices_path:
            raise TTSUnavailableError(
                "kokoro-onnx needs explicit weight paths",
                provider=PROVIDER_NAME,
                remediation=(
                    "set tts.kokoro.model_path and tts.kokoro.voices_path; download "
                    "kokoro-v1.0.onnx and voices-v1.0.bin from the kokoro-onnx releases page"
                ),
            )
        for label, raw in (("model_path", model_path), ("voices_path", voices_path)):
            if not Path(raw).expanduser().is_file():
                raise TTSUnavailableError(
                    "kokoro weight file not found", provider=PROVIDER_NAME, **{label: raw}
                )
        self._kokoro = Kokoro(
            str(Path(model_path).expanduser()), str(Path(voices_path).expanduser())
        )

    @property
    def name(self) -> str:
        return "kokoro-onnx"

    @property
    def model_version(self) -> str:
        return "kokoro-v1.0-onnx"

    def voices(self) -> tuple[str, ...]:
        getter = getattr(self._kokoro, "get_voices", None)
        if getter is None:
            return KNOWN_VOICES
        return tuple(sorted(getter()))

    def synthesize(self, text: str, *, voice: str, speed: float, language: str) -> list[float]:
        samples, rate = self._kokoro.create(
            text, voice=voice, speed=speed, lang=normalise_language(language)
        )
        if int(rate) != KOKORO_SAMPLE_RATE:  # pragma: no cover - model contract
            raise SynthesisError(
                "kokoro returned an unexpected sample rate",
                expected=KOKORO_SAMPLE_RATE,
                actual=int(rate),
            )
        return [float(value) for value in samples]


class _TorchBackend:
    """The reference `kokoro` package. Heavier install, CUDA-capable."""

    def __init__(self, *, repo_id: str, device: str, language: str = "en-us") -> None:
        from kokoro import KPipeline

        self._pipeline = KPipeline(
            lang_code=LANGUAGE_CODES.get(language.lower(), "a"),
            repo_id=repo_id or "hexgrad/Kokoro-82M",
            device=_resolve_device(device),
        )

    @property
    def name(self) -> str:
        return "kokoro-torch"

    @property
    def model_version(self) -> str:
        return "kokoro-v1.0-torch"

    def voices(self) -> tuple[str, ...]:
        return KNOWN_VOICES

    # `language` is fixed at construction for this backend, but the shared
    # backend signature still has to accept it.
    def synthesize(
        self,
        text: str,
        *,
        voice: str,
        speed: float,
        language: str,  # noqa: ARG002
    ) -> list[float]:
        samples: list[float] = []
        # The pipeline's language was fixed at construction; `language` is
        # accepted here only to satisfy the shared backend signature.
        for _graphemes, _phonemes, audio in self._pipeline(text, voice=voice, speed=speed):
            samples.extend(float(value) for value in audio)
        return samples


def normalise_language(language: str) -> str:
    """Map a configured language onto an espeak-ng code.

    espeak wants a region: it rejects bare "en" rather than assuming one. The
    assumption is made here, once and visibly, instead of surfacing as a
    RuntimeError from three libraries down.
    """
    lowered = language.strip().lower()
    if lowered in {"en", ""}:
        return "en-us"
    return lowered


def _resolve_device(device: str) -> str | None:
    """Map our config value onto what the torch backend expects."""
    if device == "auto":
        return None
    return device


def _probe_backends() -> tuple[str | None, str]:
    """Which backend is importable, and what to say when none is.

    Import errors are caught and turned into a message rather than propagated:
    a missing optional dependency is a configuration state, not a crash.
    """
    try:
        import kokoro_onnx  # noqa: F401
    except ImportError:
        pass
    else:
        return "onnx", "kokoro-onnx"

    try:
        import kokoro  # noqa: F401
    except ImportError:
        pass
    else:
        return "torch", "kokoro"

    return None, f"no kokoro backend installed ({_INSTALL_HINT})"


class KokoroProvider:
    """Local neural speech synthesis, one utterance at a time."""

    def __init__(
        self,
        *,
        repo_id: str = "hexgrad/Kokoro-82M",
        model_path: str = "",
        voices_path: str = "",
        device: str = "auto",
        language: str = "en-us",
        max_clip_seconds: float = 300.0,
    ) -> None:
        self._repo_id = repo_id
        self._model_path = model_path
        self._voices_path = voices_path
        self._device = device
        self._language = language
        self._max_clip_seconds = max_clip_seconds
        self._backend: _Backend | None = None

    @property
    def name(self) -> str:
        return PROVIDER_NAME

    @property
    def model_version(self) -> str:
        backend = self._backend
        return backend.model_version if backend else "kokoro-v1.0"

    def available(self) -> tuple[bool, str]:
        if self._backend is not None:
            return True, f"ready ({self._backend.name})"
        kind, detail = _probe_backends()
        if kind is None:
            return False, detail
        return True, f"ready ({detail}, not yet loaded)"

    def voices(self) -> tuple[str, ...]:
        try:
            return self._load().voices()
        except TTSUnavailableError:
            return KNOWN_VOICES

    def _load(self) -> _Backend:
        """Load the model on first use. Weights are never loaded at import."""
        if self._backend is not None:
            return self._backend

        kind, detail = _probe_backends()
        if kind is None:
            raise TTSUnavailableError(detail, provider=PROVIDER_NAME)

        try:
            backend: _Backend
            if kind == "onnx":
                backend = _OnnxBackend(model_path=self._model_path, voices_path=self._voices_path)
            else:
                backend = _TorchBackend(
                    repo_id=self._repo_id, device=self._device, language=self._language
                )
        except (ImportError, OSError, RuntimeError, ValueError) as exc:
            # Covers missing weight files, a corrupt download, and a CUDA
            # runtime that is present but unusable.
            raise TTSUnavailableError(
                "kokoro backend failed to load",
                provider=PROVIDER_NAME,
                backend=kind,
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

        _log.info("kokoro_loaded", backend=backend.name, model_version=backend.model_version)
        self._backend = backend
        return backend

    def synthesize(self, request: SpeechRequest, *, output_dir: Path) -> SpeechResult:
        backend = self._load()

        if request.voice_id not in backend.voices():
            raise TTSUnavailableError(
                "unknown voice",
                provider=PROVIDER_NAME,
                voice=request.voice_id,
                available=", ".join(backend.voices()[:8]),
            )

        output_dir.mkdir(parents=True, exist_ok=True)
        path = output_dir / f"{request.cache_key(model_version=self.model_version)}.wav"
        if path.is_file():
            duration = wav_duration(path)
            return SpeechResult(
                audio_path=path,
                duration_seconds=duration,
                voice_id=request.voice_id,
                model_version=self.model_version,
                sample_rate=KOKORO_SAMPLE_RATE,
                word_timings=distribute_words(request.text, start=0.0, end=duration),
                cached=True,
                metadata=dict(request.metadata),
            )

        try:
            samples = backend.synthesize(
                request.text,
                voice=request.voice_id,
                speed=request.speed,
                language=request.language,
            )
        except SynthesisError:
            raise
        except Exception as exc:  # a third-party model has an unbounded error surface
            raise SynthesisError(
                "kokoro synthesis failed",
                provider=PROVIDER_NAME,
                voice=request.voice_id,
                characters=len(request.text),
                detail=f"{type(exc).__name__}: {exc}",
            ) from exc

        if not samples:
            raise SynthesisError(
                "kokoro produced no audio", provider=PROVIDER_NAME, text_length=len(request.text)
            )

        duration = len(samples) / KOKORO_SAMPLE_RATE
        if duration > self._max_clip_seconds:
            # A model looping on malformed input can emit minutes of audio for
            # one sentence. Refuse it rather than mux it into a video.
            raise SynthesisError(
                "synthesised clip is implausibly long; refusing it",
                provider=PROVIDER_NAME,
                duration_seconds=round(duration, 1),
                limit=self._max_clip_seconds,
                characters=len(request.text),
            )

        write_wav(path, samples, sample_rate=KOKORO_SAMPLE_RATE)
        return SpeechResult(
            audio_path=path,
            duration_seconds=duration,
            voice_id=request.voice_id,
            model_version=self.model_version,
            sample_rate=KOKORO_SAMPLE_RATE,
            word_timings=distribute_words(request.text, start=0.0, end=duration),
            cached=False,
            metadata=dict(request.metadata),
        )


def float_to_pcm16(samples: Sequence[float]) -> bytes:
    """Convert float samples in [-1, 1] to little-endian 16-bit PCM.

    Values are clamped before scaling. A model occasionally emits samples
    slightly outside the range, and letting those wrap produces a loud click
    rather than a briefly clipped one.
    """
    buffer = array("h", bytes(2 * len(samples)))
    for index, value in enumerate(samples):
        clamped = -1.0 if value < -1.0 else (1.0 if value > 1.0 else float(value))
        buffer[index] = int(clamped * 32767)
    if sys.byteorder == "big":  # pragma: no cover - x86/ARM are little-endian
        buffer.byteswap()
    return buffer.tobytes()


def write_wav(path: Path, samples: Sequence[float], *, sample_rate: int) -> None:
    """Write mono 16-bit PCM."""
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(sample_rate)
        handle.writeframes(float_to_pcm16(samples))


def wav_duration(path: Path) -> float:
    with wave.open(str(path), "rb") as handle:
        return handle.getnframes() / float(handle.getframerate())
