"""The `pulpmill` command-line application.

Only commands that actually work are registered. Errors are reported as a clear
message and a non-zero exit status rather than a traceback -- the full detail is
already in the structured log.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

# Typer vendors Click, so the context type its command classes actually declare
# lives here. `typer.Context` is a *subclass*, and narrowing an override's
# parameter to it would violate the Liskov substitution principle.
from typer._click.core import Context as ClickContext
from typer.core import TyperGroup

from pulpmill import __version__
from pulpmill.cli import render
from pulpmill.cli.commands import admin_cmds, pipeline_cmds, production_cmds, report_cmds
from pulpmill.cli.context import CliContext
from pulpmill.domain.errors import ConfigError, PulpmillError

#: Exit codes. 2 is reserved for "your configuration is wrong", which is worth
#: distinguishing from "the run failed" when this is driven from a scheduler.
EXIT_FAILURE = 1
EXIT_CONFIG_ERROR = 2
EXIT_INTERRUPTED = 130


class ErrorHandlingGroup(TyperGroup):
    """Turns domain errors into a clear message and a meaningful exit code.

    Implemented on the group rather than in `main()` so the behaviour holds for
    every entry point -- the installed script, `python -m`, and tests alike.
    Tracebacks stay out of the terminal; the full detail is already in the
    structured log.
    """

    def invoke(self, ctx: ClickContext) -> Any:
        try:
            return super().invoke(ctx)
        except ConfigError as exc:
            render.fail(str(exc))
            raise typer.Exit(code=EXIT_CONFIG_ERROR) from exc
        except PulpmillError as exc:
            render.fail(f"{type(exc).__name__}: {exc}")
            raise typer.Exit(code=EXIT_FAILURE) from exc


app = typer.Typer(
    name="pulpmill",
    cls=ErrorHandlingGroup,
    help="Local-first story discovery, deduplication and ranking engine.",
    no_args_is_help=True,
    add_completion=False,
    context_settings={"help_option_names": ["-h", "--help"]},
)

pipeline_cmds.register(app)
production_cmds.register(app)
report_cmds.register(app)
admin_cmds.register(app)


def _version_callback(value: bool) -> None:
    if value:
        render.console.print(f"pulpmill {__version__}")
        raise typer.Exit()


@app.callback()
def main_callback(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Config file to use instead of the default."),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", help="Console log level: DEBUG|INFO|WARNING|ERROR."),
    ] = None,
    _version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Show version."),
    ] = False,
) -> None:
    """Shared options for every command."""
    cli = CliContext(config_path=config, log_level=log_level.upper() if log_level else None)
    ctx.obj = cli
    ctx.call_on_close(cli.close)


def main() -> None:
    """Entry point registered as the `pulpmill` script.

    Domain errors are handled by `ErrorHandlingGroup`; this only adds the
    Ctrl-C path, which Click surfaces before the group ever sees it.
    """
    try:
        app()
    except KeyboardInterrupt:
        render.warn("interrupted")
        raise SystemExit(EXIT_INTERRUPTED) from None


if __name__ == "__main__":  # pragma: no cover
    main()
