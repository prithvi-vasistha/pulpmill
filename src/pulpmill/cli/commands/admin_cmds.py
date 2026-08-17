"""Database and configuration commands."""

from __future__ import annotations

import json
from typing import Annotated

import typer
from rich.table import Table

from pulpmill.cli import render
from pulpmill.cli.context import get_context

db_app = typer.Typer(no_args_is_help=True, help="Database schema management.")
config_app = typer.Typer(no_args_is_help=True, help="Inspect effective configuration.")


@db_app.command("upgrade")
def db_upgrade(ctx: typer.Context) -> None:
    """Apply any pending migrations. Safe to run repeatedly."""
    cli = get_context(ctx)
    app_ctx = cli.app(migrate=False)
    applied = app_ctx.migrate()
    if applied:
        for label in applied:
            render.ok(f"applied {label}")
    else:
        render.ok("schema is up to date")


@db_app.command("status")
def db_status(ctx: typer.Context) -> None:
    """Show applied and pending migrations."""
    cli = get_context(ctx)
    app_ctx = cli.app(migrate=False)
    status = app_ctx.migration_status()

    table = Table(title="Migrations", title_justify="left", header_style="bold")
    table.add_column("migration")
    table.add_column("state")
    for label in status.applied:
        table.add_row(label, "[green]applied[/green]")
    for label in status.pending:
        table.add_row(label, "[yellow]pending[/yellow]")
    render.console.print(table)
    render.console.print(f"[dim]database: {app_ctx.config.database_path}[/dim]")

    if not status.is_current:
        render.warn("Pending migrations -- run `pulpmill db upgrade`.")
        raise typer.Exit(code=1)


@db_app.command("verify")
def db_verify(ctx: typer.Context) -> None:
    """Check that applied migrations still match the files on disk."""
    cli = get_context(ctx)
    app_ctx = cli.app(migrate=False)
    app_ctx.migration_runner.verify()
    render.ok("all applied migrations match their checksums")


@config_app.command("show")
def config_show(
    ctx: typer.Context,
    as_json: Annotated[bool, typer.Option("--json", help="Emit JSON.")] = False,
) -> None:
    """Print the effective configuration after all layers are merged.

    Secrets are never part of the configuration and so cannot appear here.
    """
    cli = get_context(ctx)
    app_ctx = cli.app(migrate=False)
    payload = app_ctx.config.model_dump(mode="json")

    if as_json:
        render.console.print_json(json.dumps(payload, default=str))
        return

    render.key_values(
        {
            "project root": str(app_ctx.config.project_root),
            "data dir": str(app_ctx.config.data_dir),
            "database": str(app_ctx.config.database_path),
            "log file": str(app_ctx.config.log_file_path),
            "ranking version": app_ctx.config.ranking.version,
            "config fingerprint": app_ctx.config.ranking.fingerprint(),
            "editorial provider": app_ctx.config.editorial.provider,
            "sources enabled": ", ".join(app_ctx.config.enabled_sources()) or "none",
        },
        title="Effective configuration",
    )

    weights = app_ctx.config.ranking.weights.as_mapping()
    total = sum(weights.values())
    table = Table(title="Ranking weights", title_justify="left", header_style="bold")
    table.add_column("signal")
    table.add_column("configured", justify="right")
    table.add_column("normalized", justify="right")
    for name, value in weights.items():
        table.add_row(name, f"{value:.3f}", f"{value / total:.3f}" if total else "0.000")
    render.console.print(table)


@config_app.command("secrets")
def config_secrets(ctx: typer.Context) -> None:
    """Report which credentials are configured. Never prints a value."""
    cli = get_context(ctx)
    app_ctx = cli.app(migrate=False)

    checks: tuple[tuple[str, str, bool], ...] = (
        ("reddit", "PULPMILL_REDDIT_CLIENT_ID", app_ctx.secrets.has("REDDIT_CLIENT_ID")),
        (
            "reddit",
            "PULPMILL_REDDIT_CLIENT_SECRET",
            app_ctx.secrets.has("REDDIT_CLIENT_SECRET"),
        ),
        ("reddit", "PULPMILL_REDDIT_USER_AGENT", app_ctx.secrets.has("REDDIT_USER_AGENT")),
        ("x", "PULPMILL_X_BEARER_TOKEN", app_ctx.secrets.has("X_BEARER_TOKEN")),
        (
            "editorial",
            "ANTHROPIC_API_KEY",
            app_ctx.secrets.has("ANTHROPIC_API_KEY", prefixed=False),
        ),
    )

    table = Table(title="Credentials", title_justify="left", header_style="bold")
    table.add_column("used by")
    table.add_column("variable")
    table.add_column("set", justify="center")
    for used_by, variable, present in checks:
        table.add_row(
            used_by,
            variable,
            "[green]yes[/green]" if present else "[dim]no[/dim]",
        )
    render.console.print(table)
    render.console.print(
        "[dim]Values are never read back or logged. See docs/CREDENTIALS.md.[/dim]"
    )


def register(app: typer.Typer) -> None:
    app.add_typer(db_app, name="db")
    app.add_typer(config_app, name="config")
