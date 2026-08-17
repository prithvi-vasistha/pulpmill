"""Inspection and reporting commands."""

from __future__ import annotations

import json
from typing import Annotated, Any

import typer
from rich.table import Table

from pulpmill.cli import render
from pulpmill.cli.context import get_context
from pulpmill.domain.errors import IngestionError, StoryNotFoundError
from pulpmill.ingestion.registry import AdapterContext, create_adapter, registered_adapters


def register(app: typer.Typer) -> None:
    @app.command()
    def sources(
        ctx: typer.Context,
        check: Annotated[
            bool,
            typer.Option("--check", help="Instantiate each adapter and report its health."),
        ] = True,
    ) -> None:
        """List configured sources and whether each one can currently fetch."""
        cli = get_context(ctx)
        app_ctx = cli.app()

        table = Table(title="Sources", title_justify="left", header_style="bold", expand=True)
        table.add_column("name")
        table.add_column("adapter")
        table.add_column("enabled", justify="center")
        table.add_column("rps", justify="right")
        table.add_column("queries", justify="right")
        table.add_column("state")
        table.add_column("detail", overflow="fold", ratio=2)

        for name, source_config in app_ctx.config.sources.items():
            enabled = "[green]yes[/green]" if source_config.enabled else "[dim]no[/dim]"
            state = "[dim]not checked[/dim]"
            detail = ""
            remediation: str | None = None

            if check:
                try:
                    adapter = create_adapter(
                        AdapterContext(
                            name=name,
                            config=app_ctx.config,
                            source_config=source_config,
                            secrets=app_ctx.secrets,
                            clock=app_ctx.clock,
                        )
                    )
                except IngestionError as exc:
                    state = "[red]error[/red]"
                    detail = str(exc)
                else:
                    try:
                        health = adapter.health()
                        state = (
                            "[green]ready[/green]"
                            if health.available
                            else "[yellow]unavailable[/yellow]"
                        )
                        detail = health.detail
                        remediation = health.remediation
                    finally:
                        adapter.close()

            table.add_row(
                name,
                source_config.adapter,
                enabled,
                f"{source_config.rate_limit.requests_per_second:g}",
                str(len(source_config.queries)),
                state,
                detail,
            )
            if remediation:
                table.add_row("", "", "", "", "", "", f"[dim]-> {remediation}[/dim]")

        render.console.print(table)
        render.console.print(f"[dim]registered adapters: {', '.join(registered_adapters())}[/dim]")

    @app.command()
    def status(
        ctx: typer.Context,
        top: Annotated[int, typer.Option("--top", min=0, help="Candidates to show.")] = 5,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Show pipeline state: counts by status and source, failures, recent jobs."""
        cli = get_context(ctx)
        app_ctx = cli.app()

        by_status = app_ctx.stories.count_by_status()
        by_platform = app_ctx.stories.count_by_platform()
        by_dedup_layer = app_ctx.stories.count_duplicates_by_layer()
        candidates = (
            app_ctx.rankings.top_candidates(
                ranking_version=app_ctx.ranking.version,
                config_fingerprint=app_ctx.ranking.config_fingerprint,
                limit=top,
            )
            if top
            else []
        )

        summary: dict[str, Any] = {
            "database": str(app_ctx.config.database_path),
            "ranking_version": app_ctx.ranking.version,
            "config_fingerprint": app_ctx.ranking.config_fingerprint,
            "stories_total": app_ctx.stories.count_all(),
            "stories_by_status": by_status,
            "stories_by_source": by_platform,
            "duplicates_by_layer": by_dedup_layer,
            "rankings_stored": app_ctx.rankings.count(),
            "selections": app_ctx.editorial.count_selections(),
            "selection_batches": app_ctx.editorial.count_batches(),
            "failures_total": app_ctx.failures.count(),
            "failures_last_24h": app_ctx.failures.count_since(24),
            "jobs_running": app_ctx.jobs.count_running(),
        }

        if as_json:
            summary["top_candidates"] = [
                {
                    "position": index,
                    "story_id": entry.story.id,
                    "score": entry.ranking.final_score,
                    "title": entry.story.title,
                    "source": entry.story.source_platform,
                    "url": entry.story.canonical_url,
                }
                for index, entry in enumerate(candidates, start=1)
            ]
            render.console.print_json(json.dumps(summary, default=str))
            return

        render.key_values(
            {
                "database": summary["database"],
                "ranking version": summary["ranking_version"],
                "config fingerprint": summary["config_fingerprint"][:16],
                "stories": f"{summary['stories_total']:,}",
                "rankings stored": f"{summary['rankings_stored']:,}",
                "selections": f"{summary['selections']:,}"
                f" across {summary['selection_batches']} batch(es)",
                "failures": f"{summary['failures_total']:,}"
                f" ({summary['failures_last_24h']:,} in last 24h)",
                "jobs running": summary["jobs_running"],
            },
            title="pulpmill status",
        )

        if by_status:
            render.counts_table("Stories by status", by_status, total_label="all stories")
        if by_platform:
            render.counts_table("Stories by source", by_platform, total_label="all stories")
        if by_dedup_layer:
            render.counts_table("Duplicates by layer", by_dedup_layer, total_label="all duplicates")

        recent_jobs = app_ctx.jobs.recent(5)
        if recent_jobs:
            table = Table(title="Recent jobs", title_justify="left", header_style="bold")
            table.add_column("kind")
            table.add_column("status")
            table.add_column("started")
            table.add_column("finished")
            for job in recent_jobs:
                colour = {
                    "SUCCEEDED": "green",
                    "RUNNING": "cyan",
                    "FAILED": "red",
                    "INTERRUPTED": "yellow",
                }.get(job.status.value, "white")
                table.add_row(
                    job.kind,
                    f"[{colour}]{job.status.value}[/{colour}]",
                    job.started_at,
                    job.finished_at or "-",
                )
            render.console.print(table)

        if candidates:
            render.candidates_table(candidates, title="Top candidates", show_url=False)

    @app.command()
    def top(
        ctx: typer.Context,
        count: Annotated[int, typer.Option("--count", "-n", min=1)] = 20,
        source: Annotated[
            str | None, typer.Option("--source", "-s", help="Filter to one source.")
        ] = None,
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Show the highest-ranked candidates under the current configuration."""
        cli = get_context(ctx)
        app_ctx = cli.app()
        candidates = app_ctx.rankings.top_candidates(
            ranking_version=app_ctx.ranking.version,
            config_fingerprint=app_ctx.ranking.config_fingerprint,
            limit=count,
            platform=source,
        )
        if not candidates:
            render.warn("No ranked stories for the current ranking configuration.")
            raise typer.Exit(code=1)

        if as_json:
            render.console.print_json(
                json.dumps(
                    [
                        {
                            "position": index,
                            "story_id": entry.story.id,
                            "score": entry.ranking.final_score,
                            "component_scores": dict(entry.ranking.component_scores),
                            "title": entry.story.title,
                            "source": entry.story.source_platform,
                            "url": entry.story.canonical_url,
                            "word_count": entry.story.word_count,
                        }
                        for index, entry in enumerate(candidates, start=1)
                    ],
                    default=str,
                )
            )
            return

        render.candidates_table(candidates, title=f"Top {len(candidates)} candidates")

    @app.command()
    def inspect(
        ctx: typer.Context,
        story_id: Annotated[str, typer.Argument(help="Story id (uuid).")],
        as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
    ) -> None:
        """Show one story in full: provenance, content, ranking breakdown, history."""
        cli = get_context(ctx)
        app_ctx = cli.app()

        try:
            story = app_ctx.stories.require(story_id)
        except StoryNotFoundError:
            render.fail(f"no story with id {story_id}")
            raise typer.Exit(code=1) from None

        ranking = app_ctx.rankings.latest_for_story(story.id)
        history = app_ctx.stories.history(story.id)

        if as_json:
            render.console.print_json(
                json.dumps(
                    {
                        "story": {
                            "id": story.id,
                            "source_platform": story.source_platform,
                            "source_id": story.source_id,
                            "canonical_url": story.canonical_url,
                            "author": story.author,
                            "title": story.title,
                            "status": story.status.value,
                            "word_count": story.word_count,
                            "content_hash": story.content_hash,
                            "created_at": story.created_at.isoformat(),
                            "discovered_at": story.discovered_at.isoformat(),
                            "engagement": story.engagement.to_dict(),
                            "metadata": dict(story.metadata),
                            "duplicate_of_id": story.duplicate_of_id,
                            "normalized_content": story.normalized_content,
                        },
                        "ranking": (
                            {
                                "final_score": ranking.final_score,
                                "ranking_version": ranking.ranking_version,
                                "config_fingerprint": ranking.config_fingerprint,
                                "component_scores": dict(ranking.component_scores),
                                "effective_weights": dict(ranking.effective_weights),
                                "explanation": dict(ranking.explanation),
                            }
                            if ranking
                            else None
                        ),
                        "history": history,
                    },
                    default=str,
                )
            )
            return

        render.story_panel(story)

        if ranking is not None:
            render.console.print()
            render.key_values(
                {
                    "final score": f"{ranking.final_score:.2f} / 100",
                    "ranking version": ranking.ranking_version,
                    "config fingerprint": ranking.config_fingerprint[:16],
                    "reference time": ranking.reference_time.isoformat(),
                }
            )
            render.signal_table(ranking.component_scores, ranking.explanation)
        else:
            render.warn("This story has not been ranked yet.")

        if history:
            table = Table(title="State history", title_justify="left", header_style="bold")
            table.add_column("when")
            table.add_column("from")
            table.add_column("to")
            table.add_column("stage")
            table.add_column("reason", overflow="fold")
            for event in history:
                table.add_row(
                    str(event["occurred_at"]),
                    str(event["from_status"] or "-"),
                    str(event["to_status"]),
                    str(event["stage"]),
                    str(event["reason"] or "-"),
                )
            render.console.print(table)

    @app.command()
    def failures(
        ctx: typer.Context,
        count: Annotated[int, typer.Option("--count", "-n", min=1)] = 20,
    ) -> None:
        """Show recent persisted failures with their source, stage and error."""
        cli = get_context(ctx)
        app_ctx = cli.app()
        rows = app_ctx.failures.recent(count)
        if not rows:
            render.ok("No failures recorded.")
            return

        table = Table(
            title="Recent failures", title_justify="left", header_style="bold", expand=True
        )
        table.add_column("when")
        table.add_column("source")
        table.add_column("stage")
        table.add_column("operation")
        table.add_column("error", overflow="fold", ratio=2)
        table.add_column("retries", justify="right")
        for row in rows:
            table.add_row(
                str(row["occurred_at"]),
                str(row["source_platform"] or "-"),
                str(row["stage"]),
                str(row["operation"]),
                f"{row['error_type']}: {row['error_message']}",
                str(row["retry_count"]),
            )
        render.console.print(table)
        render.counts_table("Failures by stage", app_ctx.failures.by_stage())
