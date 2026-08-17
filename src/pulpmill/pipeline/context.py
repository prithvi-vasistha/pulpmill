"""Application composition root.

One place where configuration, the database, repositories and engines are wired
together. Everything else takes what it needs as a constructor argument, which
is what keeps the ranking engine testable without a database and the adapters
testable without a network.
"""

from __future__ import annotations

import random
from pathlib import Path
from types import TracebackType

from pulpmill.config.loader import find_project_root, load_config
from pulpmill.config.models import AppConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.deduplication.engine import DeduplicationEngine
from pulpmill.infrastructure.clock import SYSTEM_CLOCK, Clock
from pulpmill.infrastructure.logging import configure_logging, get_logger
from pulpmill.persistence.database import Database
from pulpmill.persistence.migrations import MigrationRunner, MigrationStatus, default_migrations_dir
from pulpmill.persistence.repositories.editorial import EditorialRepository
from pulpmill.persistence.repositories.jobs import FailureRepository, JobRepository
from pulpmill.persistence.repositories.rankings import RankingRepository
from pulpmill.persistence.repositories.stories import StoryRepository
from pulpmill.ranking.engine import RankingEngine


class Application:
    """Wires the object graph and owns the database connection's lifetime."""

    def __init__(
        self,
        config: AppConfig,
        *,
        secrets: SecretStore | None = None,
        clock: Clock = SYSTEM_CLOCK,
        database: Database | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.config = config
        self.secrets = secrets or SecretStore.from_environment()
        self.clock = clock
        self.rng = rng
        self.log = get_logger("pipeline.application")

        self.database = database or Database(config.database_path, config.runtime.database)
        self.stories = StoryRepository(
            self.database,
            clock,
            simhash_band_count=config.deduplication.layers.near_duplicate.band_count,
        )
        self.rankings = RankingRepository(self.database, clock)
        self.jobs = JobRepository(self.database, clock)
        self.failures = FailureRepository(self.database, clock)
        self.editorial = EditorialRepository(self.database, clock)

        self.deduplication = DeduplicationEngine(config.deduplication, self.stories)
        self.ranking = RankingEngine(config)

    @classmethod
    def create(
        cls,
        *,
        project_root: Path | None = None,
        config_path: Path | None = None,
        clock: Clock = SYSTEM_CLOCK,
        configure_logs: bool = True,
        migrate: bool = True,
    ) -> Application:
        """Build an application from on-disk configuration.

        The default entry point for the CLI. Migrations run by default so a
        fresh checkout works with one command instead of two.
        """
        root = project_root or find_project_root()
        config = load_config(project_root=root, config_path=config_path)
        if configure_logs:
            configure_logging(config.runtime.logging, log_file_path=config.log_file_path)
        app = cls(config, clock=clock)
        if migrate:
            app.migrate()
        return app

    # --- schema --------------------------------------------------------------

    @property
    def migration_runner(self) -> MigrationRunner:
        return MigrationRunner(self.database, default_migrations_dir(self.config.project_root))

    def migrate(self) -> list[str]:
        return self.migration_runner.upgrade()

    def migration_status(self) -> MigrationStatus:
        return self.migration_runner.status()

    # --- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        self.database.close()

    def __enter__(self) -> Application:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
