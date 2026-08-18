"""The render stage: script + audio + captions become one MP4.

One ffmpeg invocation per video. Composing in a single pass avoids intermediate
files and re-encodes, which matters when this runs continuously on a laptop that
is also scraping and synthesising.

The filter graph, in order:

    background -> scale to cover -> crop to frame -> fps -> grain
               -> burn in captions (ASS) -> watermark overlay

Captions are burned in rather than muxed as a subtitle track because no
short-form platform displays soft subtitles, and because burned-in text is what
the format's viewers expect to see.

**Nothing scraped ever reaches the command line.** Caption and title text travel
in an ASS file; the only strings placed in arguments are paths this application
generated and numbers from validated configuration.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from pulpmill.captions.ass import write_ass
from pulpmill.config.models import CaptionConfig, RenderConfig
from pulpmill.domain.errors import AssetError, RenderError
from pulpmill.domain.media import AudioArtifact, CaptionCue, VideoArtifact, build_video_id
from pulpmill.domain.script import NarrationScript
from pulpmill.infrastructure.logging import get_logger
from pulpmill.rendering.backgrounds import (
    BackgroundProvider,
    BackgroundSource,
    ProceduralBackgroundProvider,
)
from pulpmill.rendering.ffmpeg import ffmpeg_path, probe, run, select_encoder

_log = get_logger("rendering.compositor")

#: Output audio rate. Kokoro emits 24 kHz; platforms normalise to 48 kHz, and
#: resampling once here beats letting each of them do it differently.
OUTPUT_SAMPLE_RATE = 48_000


@dataclass(frozen=True, slots=True)
class RenderOutcome:
    artifact: VideoArtifact
    #: True when an up-to-date file already existed and nothing was encoded.
    reused: bool
    captions_path: Path
    encoder: str


class VideoCompositor:
    """Renders one video per call, under one configuration."""

    def __init__(
        self,
        *,
        config: RenderConfig,
        captions: CaptionConfig,
        background: BackgroundProvider,
        output_dir: Path,
        work_dir: Path,
        production_fingerprint: str,
    ) -> None:
        self._config = config
        self._captions = captions
        self._background = background
        self._output_dir = output_dir
        self._work_dir = work_dir
        self._fingerprint = production_fingerprint

    def render(
        self,
        script: NarrationScript,
        audio: AudioArtifact,
        cues: tuple[CaptionCue, ...],
        *,
        force: bool = False,
    ) -> RenderOutcome:
        if not audio.exists():
            raise RenderError(
                "narration audio is missing", script_id=script.id, path=str(audio.path)
            )
        if audio.duration_seconds <= 0:
            raise RenderError("narration audio has no duration", script_id=script.id)

        encoder = select_encoder(self._config.encoder)
        self._output_dir.mkdir(parents=True, exist_ok=True)
        self._work_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._output_dir / f"{script.id}.mp4"

        captions_path = write_ass(
            self._work_dir / f"{script.id}.ass",
            cues,
            self._captions,
            width=self._config.width,
            height=self._config.height,
            title=script.title,
            title_card=self._config.title_card,
            title_text=self._title_text(script),
        )

        source = self._background.resolve(
            seed=script.id,
            duration_seconds=audio.duration_seconds,
            width=self._config.width,
            height=self._config.height,
            fps=self._config.fps,
        )

        if output_path.is_file() and not force:
            existing = probe(output_path)
            fresh = abs(existing.duration_seconds - audio.duration_seconds) < 0.5
            if fresh:
                _log.info("render_reused", script_id=script.id, path=output_path.name)
                return RenderOutcome(
                    artifact=self._artifact(script, audio, existing, encoder, source),
                    reused=True,
                    captions_path=captions_path,
                    encoder=encoder,
                )

        args = self._build_command(
            audio_path=audio.path,
            captions_path=captions_path,
            source=source,
            duration_seconds=audio.duration_seconds,
            encoder=encoder,
            output_path=output_path,
        )
        run(args, timeout=self._config.timeout_seconds, description="ffmpeg render")

        if not output_path.is_file():  # pragma: no cover - ffmpeg exited 0 with no file
            raise RenderError("ffmpeg reported success but wrote no file", script_id=script.id)

        info = probe(output_path)
        _log.info(
            "render_complete",
            script_id=script.id,
            story_id=script.story_id,
            encoder=encoder,
            background=source.name,
            duration_seconds=round(info.duration_seconds, 2),
            size_bytes=info.size_bytes,
            resolution=f"{info.width}x{info.height}",
        )
        return RenderOutcome(
            artifact=self._artifact(script, audio, info, encoder, source),
            reused=False,
            captions_path=captions_path,
            encoder=encoder,
        )

    # --- command construction ------------------------------------------------

    def _title_text(self, script: NarrationScript) -> str:
        """Title card text, with the part label when it is a series."""
        if not script.is_series:
            return script.title
        return f"{script.title}\\N{script.label}"

    def _build_command(
        self,
        *,
        audio_path: Path,
        captions_path: Path,
        source: BackgroundSource,
        duration_seconds: float,
        encoder: str,
        output_path: Path,
    ) -> list[str]:
        config = self._config
        args = [ffmpeg_path(), "-hide_banner", "-nostdin", "-y", "-loglevel", "error"]
        args += list(source.input_args)
        args += ["-i", str(audio_path)]

        watermark_index: int | None = None
        if config.watermark.enabled:
            args += ["-i", str(self._watermark_path())]
            watermark_index = 2

        args += ["-filter_complex", self._filter_graph(source, captions_path, watermark_index)]
        args += ["-map", "[v]", "-map", "1:a"]
        args += _encoder_args(encoder, config)
        args += [
            "-pix_fmt",
            "yuv420p",
            "-r",
            str(config.fps),
            "-c:a",
            "aac",
            "-b:a",
            config.audio_bitrate,
            "-ar",
            str(OUTPUT_SAMPLE_RATE),
            "-ac",
            "2",
            # Two-pass loudnorm would be more accurate; one pass is within about
            # a decibel and costs one decode instead of two. The platforms
            # normalise again anyway -- the point is to arrive close, not exact.
            "-af",
            f"loudnorm=I={config.loudness_lufs:g}:TP=-1.5:LRA=11",
            "-t",
            f"{duration_seconds:.3f}",
            "-movflags",
            "+faststart",
            str(output_path),
        ]
        return args

    def _filter_graph(
        self, source: BackgroundSource, captions_path: Path, watermark_index: int | None
    ) -> str:
        config = self._config
        chain = [
            f"[0:v]scale={config.width}:{config.height}"
            ":force_original_aspect_ratio=increase:flags=bicubic",
            f"crop={config.width}:{config.height}",
            f"fps={config.fps}",
            "setsar=1",
        ]

        grain = self._grain_strength(source)
        if grain > 0:
            # Temporal grain over a flat gradient; without it large smooth areas
            # band visibly after H.264 quantisation.
            chain.append(f"noise=alls={grain}:allf=t+u")

        chain.append(f"ass=filename={_escape_filter_path(captions_path)}")
        graph = ",".join(chain) + "[base]"

        if watermark_index is None:
            return graph.replace("[base]", "[v]")

        overlay_x, overlay_y = _watermark_position(config)
        graph += (
            f";[{watermark_index}:v]scale={round(config.width * config.watermark.scale)}:-1,"
            f"format=rgba,colorchannelmixer=aa={config.watermark.opacity:g}[wm]"
            f";[base][wm]overlay={overlay_x}:{overlay_y}[v]"
        )
        return graph

    def _grain_strength(self, source: BackgroundSource) -> int:
        """Grain is for generated backgrounds; real footage has its own."""
        if source.kind != "procedural":
            return 0
        provider = self._background
        if isinstance(provider, ProceduralBackgroundProvider):
            return round(provider.grain * 100)
        return round(self._config.background.procedural.grain * 100)

    def _watermark_path(self) -> Path:
        path = Path(self._config.watermark.path)
        if not path.is_absolute():
            path = self._output_dir.parent.parent / self._config.watermark.path
        if not path.is_file():
            # Enabled but absent is a configuration error, never a silently
            # skipped overlay: a batch published without branding is not
            # something to discover afterwards.
            raise AssetError(
                "watermark is enabled but the file does not exist",
                path=str(path),
                remediation="add the file, or set render.watermark.enabled: false",
            )
        return path

    def _artifact(
        self,
        script: NarrationScript,
        audio: AudioArtifact,
        info: object,
        encoder: str,
        source: BackgroundSource,
    ) -> VideoArtifact:
        from pulpmill.rendering.ffmpeg import MediaInfo

        assert isinstance(info, MediaInfo)  # narrowing for the type checker
        return VideoArtifact(
            id=build_video_id(script.id),
            script_id=script.id,
            story_id=script.story_id,
            audio_id=audio.id,
            path=info.path,
            duration_seconds=info.duration_seconds,
            width=info.width,
            height=info.height,
            fps=info.fps,
            size_bytes=info.size_bytes,
            encoder=encoder,
            background_source=source.name,
            production_fingerprint=self._fingerprint,
            provenance=script.provenance,
            metadata={
                "part_number": script.part_number,
                "total_parts": script.total_parts,
                "background_kind": source.kind,
                "background_offset": source.start_offset_seconds,
                "background_looped": source.looped,
                "audio_duration": round(audio.duration_seconds, 3),
            },
        )


def _encoder_args(encoder: str, config: RenderConfig) -> list[str]:
    """Encoder-specific quality settings.

    NVENC and libx264 spell the same intent differently: `-cq` under VBR with
    `-b:v 0` is NVENC's constant-quality mode, and `-crf` is x264's. One
    `quality` number drives both because their scales are close enough that a
    second knob would be false precision.
    """
    if encoder == "h264_nvenc":
        args = [
            "-c:v",
            "h264_nvenc",
            "-preset",
            config.preset,
            "-rc",
            "vbr",
            "-cq",
            str(config.quality),
            "-b:v",
            "0",
            "-profile:v",
            "high",
        ]
    else:
        args = [
            "-c:v",
            encoder,
            "-preset",
            config.libx264_preset,
            "-crf",
            str(config.quality),
            "-profile:v",
            "high",
        ]
    if config.max_bitrate:
        # bufsize at twice maxrate lets the encoder spend on hard passages
        # without the long-run average drifting above the ceiling.
        args += ["-maxrate", config.max_bitrate, "-bufsize", _double_rate(config.max_bitrate)]
    return args


def _double_rate(rate: str) -> str:
    """Double an ffmpeg bitrate string like `5M` or `4000k`."""
    suffix = rate[-1] if rate and rate[-1].isalpha() else ""
    digits = rate[:-1] if suffix else rate
    try:
        return f"{int(float(digits) * 2)}{suffix}"
    except ValueError as exc:
        raise RenderError("render.max_bitrate is not a valid bitrate", value=rate) from exc


def _watermark_position(config: RenderConfig) -> tuple[str, str]:
    margin = round(min(config.width, config.height) * config.watermark.margin_ratio)
    horizontal = {
        "top-left": str(margin),
        "bottom-left": str(margin),
        "top-right": f"W-w-{margin}",
        "bottom-right": f"W-w-{margin}",
    }[config.watermark.position]
    vertical = {
        "top-left": str(margin),
        "top-right": str(margin),
        "bottom-left": f"H-h-{margin}",
        "bottom-right": f"H-h-{margin}",
    }[config.watermark.position]
    return horizontal, vertical


def _escape_filter_path(path: Path) -> str:
    """Escape a path for use inside an ffmpeg filter argument.

    Filter syntax treats `:` as an option separator and `\\` as an escape, so a
    path containing either would silently truncate the filter or corrupt it.
    Quoting is not enough on its own -- the characters have to be escaped inside
    the quotes too.
    """
    escaped = str(path).replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    return f"'{escaped}'"
