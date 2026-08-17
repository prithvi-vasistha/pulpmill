"""CLI context.

The application is built lazily on first use, so commands that only print help
never open a database or configure logging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import typer

from pulpmill.domain.errors import PulpmillError
from pulpmill.pipeline.context import Application


@dataclass
class CliContext:
    config_path: Path | None = None
    log_level: str | None = None
    project_root: Path | None = None
    _app: Application | None = field(default=None, init=False, repr=False)

    def app(self, *, migrate: bool = True) -> Application:
        if self._app is None:
            import os

            if self.log_level:
                os.environ["PULPMILL_LOG_LEVEL"] = self.log_level
            self._app = Application.create(
                project_root=self.project_root,
                config_path=self.config_path,
                migrate=migrate,
            )
        return self._app

    def close(self) -> None:
        if self._app is not None:
            self._app.close()
            self._app = None


def get_context(ctx: typer.Context) -> CliContext:
    obj = ctx.obj
    if not isinstance(obj, CliContext):  # pragma: no cover - defensive
        raise PulpmillError("CLI context was not initialised")
    return obj
