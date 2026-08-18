"""Production commands: script, narrate, render, validate, publish.

Each stage is exposed separately as well as through `produce`, because the
stages have very different costs. Re-scripting a night's stories takes seconds;
re-rendering them takes hours. Being able to redo one without the others is the
difference between iterating on caption styling and re-encoding everything.
"""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from pulpmill.cli import render as ui
from pulpmill.cli.context import get_context
from pulpmill.domain.enums import StoryStatus
from pulpmill.pipeline.production import ProduceReport, ProductionRunner, StageReport
from pulpmill.publishing.service import PublishingService
from pulpmill.rendering.backgrounds import build_background_provider, describe_library
from pulpmill.rendering.ffmpeg import available as ffmpeg_available
from pulpmill.rendering.ffmpeg import available_encoders, select_encoder
from pulpmill.tts.service import build_tts_provider

LimitOption = Annotated[
    int | None, typer.Option("--limit", "-n", min=1, help="Max stories to process.")
]
ForceOption = Annotated[bool, typer.Option("--force", help="Redo work that is already done.")]
JsonOption = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _render_stage(report: StageReport) -> None:
    ui.key_values(
        {
            "considered": f"{report.considered:,}",
            "completed": f"{report.completed:,}",
            "artifacts": f"{report.artifacts:,}",
            "skipped": f"{report.skipped:,}",
            "set aside": f"{report.rejected:,}",
            "failures": f"{report.failures:,}",
            "duration": f"{report.duration_seconds:.1f}s",
        },
        title=report.stage.capitalize(),
    )
    for note in report.notes[:10]:
        ui.warn(note)


def _render_produce(report: ProduceReport) -> None:
    for stage in report.stages:
        _render_stage(stage)


def register(app: typer.Typer) -> None:
    @app.command()
    def script(ctx: typer.Context, limit: LimitOption = None, force: ForceOption = False) -> None:
        """Turn selected stories into narration scripts.

        Splits long stories into numbered parts. Part numbering is computed by
        the pipeline; a script provider only advises on where to cut.
        """
        cli = get_context(ctx)
        _render_stage(ProductionRunner(cli.app()).script(limit=limit, force=force))

    @app.command()
    def narrate(
        ctx: typer.Context,
        limit: LimitOption = None,
        force: ForceOption = False,
        provider: Annotated[
            str | None,
            typer.Option("--provider", help="Override the configured TTS provider."),
        ] = None,
    ) -> None:
        """Synthesise narration audio for scripted stories.

        Uses the configured TTS provider. Clips are cached by content, so
        re-running after a hook change re-synthesises only what changed.

        `--provider mock` writes silence of the correct length, which exercises
        the render stage without a model. Videos made that way are silent and
        will be refused by `validate`, which is the point.
        """
        cli = get_context(ctx)
        _render_stage(
            ProductionRunner(cli.app()).narrate(limit=limit, force=force, provider=provider)
        )

    @app.command(name="render")
    def render_videos(
        ctx: typer.Context, limit: LimitOption = None, force: ForceOption = False
    ) -> None:
        """Compose vertical video from narration, captions and a background."""
        cli = get_context(ctx)
        _render_stage(ProductionRunner(cli.app()).render(limit=limit, force=force))

    @app.command()
    def validate(ctx: typer.Context, limit: LimitOption = None) -> None:
        """Check rendered files against the publishability rules.

        A file that fails is not published. Every check is recorded with its
        measured value, whether it passed or not.
        """
        cli = get_context(ctx)
        _render_stage(ProductionRunner(cli.app()).validate(limit=limit))

    @app.command()
    def produce(
        ctx: typer.Context,
        limit: LimitOption = None,
        force: ForceOption = False,
        as_json: JsonOption = False,
    ) -> None:
        """Run script, narrate, render and validate in order."""
        cli = get_context(ctx)
        report = ProductionRunner(cli.app()).produce(limit=limit, force=force)
        if as_json:
            ui.console.print_json(json.dumps(report.as_dict()))
        else:
            _render_produce(report)

    @app.command()
    def publish(
        ctx: typer.Context,
        story: Annotated[
            str | None, typer.Option("--story", help="Publish one story by id.")
        ] = None,
        targets: Annotated[
            list[str] | None,
            typer.Option("--target", "-t", help="Limit to these targets. Repeatable."),
        ] = None,
        limit: LimitOption = None,
        live: Annotated[
            bool,
            typer.Option(
                "--live",
                help="Actually transmit. Without this, requests are built and validated only.",
            ),
        ] = False,
    ) -> None:
        """Publish validated videos to the configured platforms.

        Dry run unless --live is given: every request is built, validated and
        recorded, but nothing is transmitted. Publishing is irreversible, so the
        safe path is the default one.
        """
        cli = get_context(ctx)
        app_ctx = cli.app()
        service = PublishingService(
            config=app_ctx.config,
            secrets=app_ctx.secrets,
            stories=app_ctx.stories,
            publications=app_ctx.publications,
            validations=app_ctx.validations,
            clock=app_ctx.clock,
        )

        if live:
            ui.warn("--live: uploads will be transmitted to the configured platforms")

        stories = (
            [app_ctx.stories.require(story)]
            if story
            else list(app_ctx.stories.iter_by_status((StoryStatus.VALIDATED,)))[: limit or 10]
        )
        if not stories:
            ui.warn("Nothing validated and ready to publish.")
            raise typer.Exit(code=1)

        table = Table(title="Publishing", title_justify="left", header_style="bold")
        for column in ("story", "part", "target", "state", "detail"):
            table.add_column(column, overflow="fold")

        for item in stories:
            for script_row in app_ctx.scripts.for_story(item.id, item.provenance):
                video = app_ctx.videos.for_script(script_row.id, item.provenance)
                if video is None:
                    continue
                report = service.publish(
                    video,
                    script_row,
                    target_names=targets,
                    dry_run=not live,
                )
                for note in report.notes:
                    ui.warn(f"{item.id[:8]}: {note}")
                for name, result in report.results:
                    table.add_row(
                        item.id[:8],
                        f"{script_row.part_number}/{script_row.total_parts}",
                        name,
                        result.state.value,
                        result.detail or "-",
                    )
        ui.console.print(table)
        if not live:
            ui.console.print(
                "[dim]dry run -- nothing was transmitted. Use --live to publish.[/dim]"
            )

    @app.command()
    def targets(ctx: typer.Context) -> None:
        """Show configured publishing targets and whether each can publish."""
        cli = get_context(ctx)
        app_ctx = cli.app()
        service = PublishingService(
            config=app_ctx.config,
            secrets=app_ctx.secrets,
            stories=app_ctx.stories,
            publications=app_ctx.publications,
            validations=app_ctx.validations,
            clock=app_ctx.clock,
        )
        table = Table(title="Publishing targets", title_justify="left", header_style="bold")
        for column in ("target", "adapter", "privacy", "daily limit", "state", "detail"):
            table.add_column(column, overflow="fold")

        health = service.health()
        for name, target in app_ctx.config.publishing.targets.items():
            status = health[name]
            state = "[green]ready[/green]" if status.available else "[yellow]blocked[/yellow]"
            table.add_row(
                name,
                target.adapter,
                target.privacy,
                str(target.daily_limit),
                state,
                status.detail,
            )
        ui.console.print(table)

        for name, status in health.items():
            if status.remediation:
                ui.console.print(f"[dim]{name}: {status.remediation}[/dim]")

        if app_ctx.config.publishing.dry_run:
            ui.console.print(
                "[dim]publishing.dry_run is true -- `publish --live` is required to transmit.[/dim]"
            )

    @app.command()
    def assets(ctx: typer.Context) -> None:
        """Show render assets and toolchain readiness."""
        cli = get_context(ctx)
        config = cli.app().config

        ok, detail = ffmpeg_available()
        encoder = select_encoder(config.render.encoder) if ok else "unavailable"
        provider = build_background_provider(
            config.render.background, library_dir=config.background_library_dir
        )
        background_ready, background_detail = provider.available()
        tts = build_tts_provider(config)
        tts_ready, tts_detail = tts.available()
        watermark = config.resolve(config.render.watermark.path)

        ui.key_values(
            {
                "ffmpeg": detail,
                "encoder": encoder,
                "nvenc": "yes" if "h264_nvenc" in available_encoders() else "no",
                "frame": f"{config.render.width}x{config.render.height} @ {config.render.fps}fps",
                "tts provider": f"{tts.name} -- {tts_detail}",
                "background": background_detail,
                "watermark": (
                    f"enabled, {watermark}"
                    if config.render.watermark.enabled
                    else f"disabled (expects {watermark})"
                ),
                "video output": str(config.video_output_dir),
            },
            title="Render assets",
        )

        clips = describe_library(provider)
        if clips:
            table = Table(title="Background clips", title_justify="left", header_style="bold")
            table.add_column("clip")
            for line in clips[:20]:
                table.add_row(line)
            ui.console.print(table)
        elif not background_ready:
            ui.warn(
                f"No gameplay footage yet. Drop .mp4 files into "
                f"{config.background_library_dir} and rendering switches to them automatically."
            )

        if not tts_ready:
            ui.warn(f"TTS: {tts_detail}")
