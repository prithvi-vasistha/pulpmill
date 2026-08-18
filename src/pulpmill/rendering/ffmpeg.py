"""Running ffmpeg and ffprobe safely.

Three rules hold everywhere in this module:

* **No shell.** Every invocation is an argument list. Scraped text never reaches
  a command line at all -- captions travel via an ASS file and titles via that
  same file, so there is nothing to escape and nothing to get wrong.
* **Bounded.** Every call has a timeout. An ffmpeg process that hangs on a
  malformed input would otherwise hold the GPU for the life of the worker.
* **Diagnosable.** A failure carries the command, the exit code and the tail of
  stderr. ffmpeg puts the actual reason in its last few lines and floods the
  rest; storing all of it would swamp the log and storing none of it makes
  failures unfixable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from pulpmill.domain.errors import FFmpegError
from pulpmill.infrastructure.logging import get_logger

_log = get_logger("rendering.ffmpeg")

#: Lines of stderr kept on a failure. Enough for ffmpeg's error plus context.
STDERR_TAIL_LINES = 12


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """What ffprobe reports about a file."""

    path: Path
    duration_seconds: float
    width: int
    height: int
    fps: float
    size_bytes: int
    has_audio: bool
    video_codec: str
    audio_codec: str | None

    @property
    def is_portrait(self) -> bool:
        return self.height > self.width


def ffmpeg_path() -> str:
    binary = shutil.which("ffmpeg")
    if binary is None:
        raise FFmpegError("ffmpeg is not installed or not on PATH")
    return binary


def ffprobe_path() -> str:
    binary = shutil.which("ffprobe")
    if binary is None:
        raise FFmpegError("ffprobe is not installed or not on PATH")
    return binary


def available() -> tuple[bool, str]:
    """Whether rendering can run at all. Never raises."""
    if shutil.which("ffmpeg") is None:
        return False, "ffmpeg is not installed or not on PATH"
    if shutil.which("ffprobe") is None:
        return False, "ffprobe is not installed or not on PATH"
    return True, f"ready ({ffmpeg_version()})"


@lru_cache(maxsize=1)
def ffmpeg_version() -> str:
    try:
        completed = subprocess.run(  # argument list, fixed binary, never a shell string
            [ffmpeg_path(), "-hide_banner", "-version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    first = completed.stdout.splitlines()[0] if completed.stdout else ""
    return first.split(" Copyright")[0].strip() or "unknown"


@lru_cache(maxsize=1)
def available_encoders() -> frozenset[str]:
    """Video encoder names this ffmpeg build supports.

    Cached: the answer cannot change while the process runs, and probing once
    per render would add a process spawn to every video.
    """
    try:
        completed = subprocess.run(  # argument list, fixed binary, never a shell string
            [ffmpeg_path(), "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError, FFmpegError):
        return frozenset()

    names: set[str] = set()
    for line in completed.stdout.splitlines():
        parts = line.split()
        # Encoder lines look like " V....D h264_nvenc  NVIDIA NVENC H.264 ..."
        if len(parts) >= 2 and parts[0].startswith("V") and len(parts[0]) == 6:
            names.add(parts[1])
    return frozenset(names)


def select_encoder(preference: str) -> str:
    """Resolve the configured encoder to one this build actually has.

    `auto` prefers NVENC. On this hardware that moves encoding off the CPU
    entirely, which matters when the machine is also scraping and synthesising.
    Falling back to libx264 is not a degradation worth failing over.
    """
    encoders = available_encoders()
    if preference != "auto":
        if preference not in encoders:
            raise FFmpegError(
                "configured encoder is not available in this ffmpeg build",
                command=preference,
                stderr_tail=f"available: {', '.join(sorted(encoders)[:12])}",
            )
        return preference
    if "h264_nvenc" in encoders:
        return "h264_nvenc"
    if "libx264" in encoders:
        return "libx264"
    raise FFmpegError("no usable H.264 encoder found (need h264_nvenc or libx264)")


def run(args: Sequence[str], *, timeout: float, description: str) -> str:
    """Run an ffmpeg-family command, returning stdout.

    Raises `FFmpegError` on a non-zero exit, a timeout, or a missing binary.
    """
    command = list(args)
    _log.debug("ffmpeg_invoke", operation=description, argc=len(command))
    try:
        completed = subprocess.run(  # argument list, never a shell string
            command,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise FFmpegError(
            "ffmpeg timed out",
            command=description,
            stderr_tail=f"exceeded {timeout:g}s",
        ) from exc
    except OSError as exc:
        raise FFmpegError("could not execute ffmpeg", command=description) from exc

    if completed.returncode != 0:
        raise FFmpegError(
            f"{description} failed",
            command=" ".join(command[:6]) + (" ..." if len(command) > 6 else ""),
            returncode=completed.returncode,
            stderr_tail=_tail(completed.stderr),
        )
    return completed.stdout


def _tail(stderr: str) -> str:
    lines = [line for line in (stderr or "").splitlines() if line.strip()]
    return "\n".join(lines[-STDERR_TAIL_LINES:])


def probe(path: Path, *, timeout: float = 30.0) -> MediaInfo:
    """Read a media file's properties."""
    if not path.is_file():
        raise FFmpegError("cannot probe a file that does not exist", command=str(path))

    output = run(
        [
            ffprobe_path(),
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_format",
            "-show_streams",
            str(path),
        ],
        timeout=timeout,
        description="ffprobe",
    )
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as exc:
        raise FFmpegError("ffprobe returned output that is not JSON", command=str(path)) from exc

    streams = payload.get("streams") or []
    video = next((item for item in streams if item.get("codec_type") == "video"), None)
    audio = next((item for item in streams if item.get("codec_type") == "audio"), None)
    fmt = payload.get("format") or {}

    if video is None:
        raise FFmpegError("file contains no video stream", command=str(path))

    return MediaInfo(
        path=path,
        duration_seconds=_as_float(fmt.get("duration")) or _as_float(video.get("duration")) or 0.0,
        width=int(video.get("width") or 0),
        height=int(video.get("height") or 0),
        fps=_parse_rate(str(video.get("avg_frame_rate") or video.get("r_frame_rate") or "0/1")),
        size_bytes=int(_as_float(fmt.get("size")) or path.stat().st_size),
        has_audio=audio is not None,
        video_codec=str(video.get("codec_name") or "unknown"),
        audio_codec=str(audio.get("codec_name")) if audio else None,
    )


def measure_loudness(path: Path, *, timeout: float = 120.0) -> dict[str, float]:
    """Mean and peak volume in dBFS, via ffmpeg's volumedetect.

    Used by validation to catch a silent or clipped audio track. `volumedetect`
    reports to stderr, which is why this runs the command directly rather than
    through `run` -- a successful measurement has nothing on stdout.
    """
    args = [
        ffmpeg_path(),
        "-hide_banner",
        "-nostats",
        "-i",
        str(path),
        "-map",
        "0:a:0",
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    try:
        completed = subprocess.run(  # argument list, never a shell string
            args, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise FFmpegError("loudness measurement failed", command=str(path)) from exc

    if completed.returncode != 0:
        raise FFmpegError(
            "loudness measurement failed",
            command=str(path),
            returncode=completed.returncode,
            stderr_tail=_tail(completed.stderr),
        )

    measured: dict[str, float] = {}
    for line in completed.stderr.splitlines():
        for key, label in (
            ("mean_volume:", "mean_volume_dbfs"),
            ("max_volume:", "max_volume_dbfs"),
        ):
            if key in line:
                value = line.split(key, 1)[1].replace("dB", "").strip()
                parsed = _as_float(value)
                if parsed is not None:
                    measured[label] = parsed
    return measured


def _as_float(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _parse_rate(rate: str) -> float:
    """Parse ffprobe's `num/den` frame rate."""
    numerator, _, denominator = rate.partition("/")
    try:
        den = float(denominator) if denominator else 1.0
        return float(numerator) / den if den else 0.0
    except ValueError:
        return 0.0
