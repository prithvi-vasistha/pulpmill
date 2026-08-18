"""Publishability checks for a rendered file.

This is the gate that stops a bad batch reaching a platform. It runs against the
*file*, not against the pipeline's beliefs about the file -- everything here is
measured with ffprobe and ffmpeg, because the failures worth catching are
exactly the ones where the pipeline thinks it succeeded.

Every check records its measured value whether it passed or failed. "What was
the loudness when it passed" is a question worth being able to answer, and a
checks table that only lists failures cannot answer it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pulpmill.config.models import RenderConfig, ValidationConfig
from pulpmill.domain.errors import FFmpegError
from pulpmill.rendering.ffmpeg import MediaInfo, measure_loudness, probe


@dataclass(frozen=True, slots=True)
class Check:
    """One measurement and its verdict."""

    name: str
    passed: bool
    value: Any
    expected: str
    detail: str = ""

    def describe(self) -> str:
        return f"{self.name}: {self.value} (expected {self.expected})"


@dataclass(frozen=True, slots=True)
class ValidationReport:
    video_path: Path
    checks: tuple[Check, ...]

    @property
    def passed(self) -> bool:
        return all(check.passed for check in self.checks)

    @property
    def failures(self) -> tuple[str, ...]:
        return tuple(check.describe() for check in self.checks if not check.passed)

    def as_dict(self) -> dict[str, Any]:
        return {
            check.name: {
                "passed": check.passed,
                "value": check.value,
                "expected": check.expected,
            }
            for check in self.checks
        }


def validate_file(
    path: Path,
    *,
    config: ValidationConfig,
    render: RenderConfig,
    expected_duration: float | None = None,
) -> ValidationReport:
    """Measure a rendered file against the publishability rules.

    Never raises for a *failing* file -- that is a verdict, not an error. It does
    raise `FFmpegError` when the file cannot be measured at all, because that is
    a different problem and should not be recorded as "invalid content".
    """
    checks: list[Check] = []

    if not path.is_file():
        return ValidationReport(
            video_path=path,
            checks=(Check("file_exists", False, "missing", "a readable file"),),
        )
    checks.append(Check("file_exists", True, path.name, "a readable file"))

    info = probe(path)
    checks.append(_duration_check(info, config))
    checks.append(_size_check(info, config))
    checks.extend(_frame_checks(info, config, render))

    if expected_duration is not None:
        drift = abs(info.duration_seconds - expected_duration)
        checks.append(
            Check(
                name="duration_matches_narration",
                passed=drift <= config.duration_tolerance_seconds,
                value=f"{drift:.2f}s drift",
                expected=f"within {config.duration_tolerance_seconds:g}s of "
                f"{expected_duration:.2f}s",
            )
        )

    checks.extend(_audio_checks(path, info, config))
    return ValidationReport(video_path=path, checks=tuple(checks))


def _duration_check(info: MediaInfo, config: ValidationConfig) -> Check:
    within = config.min_seconds <= info.duration_seconds <= config.max_seconds
    return Check(
        name="duration",
        passed=within,
        value=f"{info.duration_seconds:.2f}s",
        expected=f"{config.min_seconds:g}-{config.max_seconds:g}s",
        detail="platform ceilings: Shorts, Reels and TikTok all accept 3 minutes",
    )


def _size_check(info: MediaInfo, config: ValidationConfig) -> Check:
    return Check(
        name="file_size",
        passed=0 < info.size_bytes <= config.max_bytes,
        value=f"{info.size_bytes / 1_048_576:.1f} MiB",
        expected=f"<= {config.max_bytes / 1_048_576:.0f} MiB",
    )


def _frame_checks(
    info: MediaInfo, config: ValidationConfig, render: RenderConfig
) -> Sequence[Check]:
    checks = [
        Check(
            name="portrait_orientation",
            passed=info.is_portrait,
            value=f"{info.width}x{info.height}",
            expected="height greater than width",
        )
    ]
    if config.require_expected_dimensions:
        matches = info.width == render.width and info.height == render.height
        checks.append(
            Check(
                name="dimensions",
                passed=matches,
                value=f"{info.width}x{info.height}",
                expected=f"{render.width}x{render.height}",
            )
        )
    checks.append(
        Check(
            name="frame_rate",
            passed=info.fps > 0,
            value=f"{info.fps:.2f}",
            expected="greater than zero",
        )
    )
    return checks


def _audio_checks(path: Path, info: MediaInfo, config: ValidationConfig) -> Sequence[Check]:
    if not config.require_audio:
        return ()

    checks = [
        Check(
            name="has_audio",
            passed=info.has_audio,
            value=info.audio_codec or "none",
            expected="an audio stream",
        )
    ]
    if not info.has_audio:
        return checks

    try:
        loudness = measure_loudness(path)
    except FFmpegError as exc:
        # Measurement failed, which is not the same as the audio being bad.
        # Recorded as a failed check rather than swallowed or raised.
        return [
            *checks,
            Check(
                name="loudness",
                passed=False,
                value="unmeasurable",
                expected=f"mean above {config.min_mean_volume_dbfs:g} dBFS",
                detail=str(exc),
            ),
        ]

    mean = loudness.get("mean_volume_dbfs")
    checks.append(
        Check(
            name="not_silent",
            passed=mean is not None and mean > config.min_mean_volume_dbfs,
            value=f"{mean:.1f} dBFS" if mean is not None else "unknown",
            expected=f"above {config.min_mean_volume_dbfs:g} dBFS",
            detail="a silent track almost always means a muxing mistake",
        )
    )
    peak = loudness.get("max_volume_dbfs")
    if peak is not None:
        checks.append(
            Check(
                name="not_clipping",
                passed=peak <= 0.0,
                value=f"{peak:.1f} dBFS",
                expected="at or below 0 dBFS",
            )
        )
    return checks
