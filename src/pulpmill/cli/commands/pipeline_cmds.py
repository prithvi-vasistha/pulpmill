"""Pipeline execution commands."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from pulpmill.cli import render
from pulpmill.cli.context import get_context
from pulpmill.domain.enums import StoryStatus
from pulpmill.editorial.service import EditorialSelector, build_provider
from pulpmill.ingestion.base import RAW_FORMAT_KEY, build_story
from pulpmill.normalization.text import clean_text
from pulpmill.pipeline.reports import IngestReport, RankReport
from pulpmill.pipeline.runner import PipelineRunner

#: Statuses `renormalize` will rewrite. Everything the pipeline still owns.
_RENORMALIZABLE = (
    StoryStatus.DISCOVERED,
    StoryStatus.NORMALIZED,
    StoryStatus.DEDUPLICATED,
    StoryStatus.RANKED,
    StoryStatus.SELECTED,
    StoryStatus.DUPLICATE,
    StoryStatus.REJECTED,
)

SourceOption = Annotated[
    list[str] | None,
    typer.Option("--source", "-s", help="Limit to these sources. Repeatable."),
]
LimitOption = Annotated[
    int | None,
    typer.Option("--limit", "-n", min=1, help="Max stories to fetch per source."),
]
PagesOption = Annotated[
    int | None,
    typer.Option("--pages", min=1, help="Max pages to request per query."),
]
JsonOption = Annotated[bool, typer.Option("--json", help="Emit machine-readable JSON.")]


def _render_ingest(report: IngestReport) -> None:
    table = Table(title="Ingestion", title_justify="left", header_style="bold", expand=False)
    table.add_column("source")
    for column in ("fetched", "new", "known", "duplicates", "filtered", "failures"):
        table.add_column(column, justify="right")
    table.add_column("status")

    for name, source in report.sources.items():
        status = "[green]ok[/green]" if source.available else "[yellow]skipped[/yellow]"
        table.add_row(
            name,
            f"{source.fetched:,}",
            f"{source.new:,}",
            f"{source.known:,}",
            f"{source.duplicates:,}",
            f"{source.filtered:,}",
            f"{source.failures:,}",
            status,
        )
    render.console.print(table)

    for name, source in report.sources.items():
        if not source.available:
            render.warn(f"{name}: {source.detail}")
            if source.remediation:
                render.console.print(f"  [dim]{source.remediation}[/dim]")


def _render_rank(report: RankReport) -> None:
    render.key_values(
        {
            "ranking version": report.ranking_version,
            "config fingerprint": report.config_fingerprint[:16],
            "reference time": report.reference_time,
            "considered": f"{report.considered:,}",
            "ranked": f"{report.ranked:,}",
            "already scored": f"{report.skipped:,}",
            "novelty corpus": f"{report.corpus_size:,}",
            "failures": f"{report.failures:,}",
        },
        title="Ranking",
    )


def register(app: typer.Typer) -> None:
    @app.command()
    def run(
        ctx: typer.Context,
        sources: SourceOption = None,
        limit: LimitOption = None,
        pages: PagesOption = None,
        top: Annotated[int, typer.Option("--top", min=1, help="Candidates to display.")] = 10,
        force_rank: Annotated[
            bool, typer.Option("--force-rank", help="Re-score stories already ranked.")
        ] = False,
        as_json: JsonOption = False,
    ) -> None:
        """Fetch, normalize, deduplicate, persist, rank, and show the top candidates.

        Safe to re-run: already-known stories are refreshed rather than
        duplicated, and stories already scored under the current ranking
        configuration are skipped unless --force-rank is given.
        """
        cli = get_context(ctx)
        app_ctx = cli.app()
        runner = PipelineRunner(app_ctx)

        report = runner.run(sources=sources, limit=limit, max_pages=pages, force_rank=force_rank)
        candidates = app_ctx.rankings.top_candidates(
            ranking_version=app_ctx.ranking.version,
            config_fingerprint=app_ctx.ranking.config_fingerprint,
            limit=top,
        )

        if as_json:
            payload = report.as_dict()
            payload["top_candidates"] = [
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
            render.console.print_json(json.dumps(payload))
            return

        _render_ingest(report.ingest)
        _render_rank(report.rank)
        if candidates:
            render.console.print()
            render.candidates_table(candidates)
        else:
            render.warn("No ranked candidates yet.")

    @app.command()
    def scrape(
        ctx: typer.Context,
        sources: SourceOption = None,
        limit: LimitOption = None,
        pages: PagesOption = None,
        as_json: JsonOption = False,
    ) -> None:
        """Fetch, normalize, deduplicate and persist -- without ranking."""
        cli = get_context(ctx)
        report = PipelineRunner(cli.app()).ingest(sources=sources, limit=limit, max_pages=pages)
        if as_json:
            render.console.print_json(json.dumps(report.as_dict()))
        else:
            _render_ingest(report)

    @app.command()
    def rank(
        ctx: typer.Context,
        limit: Annotated[
            int | None, typer.Option("--limit", "-n", min=1, help="Max stories to score.")
        ] = None,
        force: Annotated[
            bool, typer.Option("--force", help="Re-score stories already ranked.")
        ] = False,
        as_json: JsonOption = False,
    ) -> None:
        """Score every deduplicated story with the configured ranking engine."""
        cli = get_context(ctx)
        report = PipelineRunner(cli.app()).rank(limit=limit, force=force)
        if as_json:
            render.console.print_json(json.dumps(report.as_dict()))
        else:
            _render_rank(report)

    @app.command()
    def dedupe(ctx: typer.Context) -> None:
        """Re-check stored stories against the current deduplication settings.

        Useful after enabling a layer or changing a threshold. Only ever adds
        duplicate links; it never un-marks an existing duplicate.
        """
        cli = get_context(ctx)
        app_ctx = cli.app()
        render.console.print(f"[dim]layers: {', '.join(app_ctx.deduplication.layers)}[/dim]")
        stats = PipelineRunner(app_ctx).rededuplicate()
        render.key_values(
            {
                "examined": f"{stats['examined']:,}",
                "newly marked duplicate": f"{stats['marked']:,}",
                "failures": f"{stats['failures']:,}",
            },
            title="Deduplication sweep",
        )

    @app.command()
    def renormalize(
        ctx: typer.Context,
        dry_run: Annotated[
            bool, typer.Option("--dry-run", help="Report changes without writing.")
        ] = False,
    ) -> None:
        """Recompute normalized text, hashes and fingerprints for stored stories.

        Run after changing the text normalizer. Provenance columns are never
        touched -- the canonical URL of a story cannot change.
        """
        cli = get_context(ctx)
        app_ctx = cli.app()

        examined = 0
        changed = 0
        for story in app_ctx.stories.iter_by_status(_RENORMALIZABLE):
            examined += 1
            # How to decode the body is recorded by the adapter at ingest time,
            # so this stays source-agnostic.
            raw_format = str(story.metadata.get(RAW_FORMAT_KEY) or "plain")
            refreshed_text = clean_text(
                story.raw_content,
                markdown=raw_format == "markdown",
                html_source=raw_format == "html",
            )
            rebuilt = build_story(
                platform=story.source_platform,
                source_id=story.source_id,
                canonical_url=story.canonical_url,
                title=story.title,
                raw_content=story.raw_content,
                normalized_content=refreshed_text,
                created_at=story.created_at,
                discovered_at=story.discovered_at,
                engagement=story.engagement,
                metadata=story.metadata,
                author=story.author,
                language=story.language,
                simhash_min_tokens=(app_ctx.config.deduplication.layers.near_duplicate.min_tokens),
            )
            if rebuilt.content_hash == story.content_hash:
                continue
            changed += 1
            if not dry_run:
                app_ctx.stories.update_normalization(rebuilt)

        render.key_values(
            {
                "examined": f"{examined:,}",
                "changed": f"{changed:,}",
                "written": "no (dry run)" if dry_run else "yes",
            },
            title="Renormalization",
        )

    @app.command()
    def select(
        ctx: typer.Context,
        count: Annotated[
            int | None, typer.Option("--count", "-c", min=1, help="Stories to select.")
        ] = None,
        pool: Annotated[
            int | None, typer.Option("--pool", min=1, help="Candidate pool size.")
        ] = None,
        provider: Annotated[
            str | None,
            typer.Option("--provider", help="Override the configured provider."),
        ] = None,
    ) -> None:
        """Pick the next batch to publish from the top-ranked candidates.

        Uses the configured editorial provider. If that provider is unavailable
        or fails for any reason, the deterministic ranking order is used and the
        reason is recorded on the batch.
        """
        cli = get_context(ctx)
        app_ctx = cli.app()

        selector = EditorialSelector(
            config=app_ctx.config,
            stories=app_ctx.stories,
            rankings=app_ctx.rankings,
            editorial=app_ctx.editorial,
            provider=build_provider(app_ctx.config, app_ctx.secrets, name=provider),
        )
        result = selector.select(
            ranking_version=app_ctx.ranking.version,
            config_fingerprint=app_ctx.ranking.config_fingerprint,
            pool_size=pool,
            count=count,
        )
        if result is None:
            render.warn("Nothing ranked yet -- run `pulpmill run` first.")
            raise typer.Exit(code=1)

        if result.used_fallback:
            render.warn(
                f"provider '{result.provider}' unavailable, used "
                f"'{result.effective_provider}': {result.fallback_reason}"
            )
        else:
            render.ok(f"selected by '{result.effective_provider}'")

        table = Table(
            title=f"Batch {result.batch_id[:8]}", title_justify="left", header_style="bold"
        )
        table.add_column("#", justify="right", width=3)
        table.add_column("story id", width=36, style="dim")
        table.add_column("rationale", overflow="fold")
        for item in result.decision.selections:
            table.add_row(str(item.position), item.story_id, item.rationale or "-")
        render.console.print(table)
