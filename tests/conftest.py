"""Shared fixtures.

No test in this suite touches the network. Adapters are exercised through an
injected `httpx.MockTransport`, which means the real fetch, pagination,
normalization and error paths run against recorded payloads.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from pulpmill.config.loader import find_project_root, load_config
from pulpmill.config.models import AppConfig
from pulpmill.config.secrets import SecretStore
from pulpmill.domain.story import Engagement, Story
from pulpmill.ingestion.base import QUALITY_KEY, RAW_FORMAT_KEY, build_story
from pulpmill.persistence.database import Database
from pulpmill.persistence.migrations import MigrationRunner, default_migrations_dir
from pulpmill.persistence.repositories.editorial import EditorialRepository
from pulpmill.persistence.repositories.jobs import FailureRepository, JobRepository
from pulpmill.persistence.repositories.rankings import RankingRepository
from pulpmill.persistence.repositories.stories import StoryRepository
from pulpmill.pipeline.context import Application
from tests.support.clock import ManualClock

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="session")
def project_root() -> Path:
    return find_project_root(Path(__file__).parent)


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def config(project_root: Path, tmp_path: Path) -> AppConfig:
    """The real committed configuration, with every write redirected to tmp.

    `project_root` stays pointed at the checkout so migrations resolve, while
    the database and log file live under the test's temporary directory. Tests
    therefore exercise the configuration that actually ships.
    """
    loaded = load_config(project_root=project_root, environ={}, load_dotenv=False)
    runtime = loaded.runtime
    return loaded.model_copy(
        update={
            "runtime": runtime.model_copy(
                update={
                    "data_dir": str(tmp_path),
                    "database": runtime.database.model_copy(
                        update={"path": str(tmp_path / "pulpmill.db")}
                    ),
                    "logging": runtime.logging.model_copy(
                        update={
                            "file": runtime.logging.file.model_copy(
                                update={
                                    "enabled": False,
                                    "path": str(tmp_path / "logs" / "pulpmill.jsonl"),
                                }
                            )
                        }
                    ),
                }
            )
        }
    )


@pytest.fixture
def database(config: AppConfig) -> Iterator[Database]:
    db = Database(config.database_path, config.runtime.database)
    MigrationRunner(db, default_migrations_dir(config.project_root)).upgrade()
    yield db
    db.close()


@pytest.fixture
def stories(database: Database, clock: ManualClock, config: AppConfig) -> StoryRepository:
    return StoryRepository(
        database,
        clock,
        simhash_band_count=config.deduplication.layers.near_duplicate.band_count,
    )


@pytest.fixture
def rankings(database: Database, clock: ManualClock) -> RankingRepository:
    return RankingRepository(database, clock)


@pytest.fixture
def jobs(database: Database, clock: ManualClock) -> JobRepository:
    return JobRepository(database, clock)


@pytest.fixture
def failures(database: Database, clock: ManualClock) -> FailureRepository:
    return FailureRepository(database, clock)


@pytest.fixture
def editorial(database: Database, clock: ManualClock) -> EditorialRepository:
    return EditorialRepository(database, clock)


@pytest.fixture
def secrets() -> SecretStore:
    """An empty secret store -- nothing is configured unless a test says so."""
    return SecretStore(environ={})


@pytest.fixture
def app(
    config: AppConfig, clock: ManualClock, secrets: SecretStore, database: Database
) -> Iterator[Application]:
    application = Application(config, secrets=secrets, clock=clock, database=database)
    yield application
    # `database` owns the connection lifetime; do not close it twice.


StoryFactory = Callable[..., Story]


@pytest.fixture
def make_story(clock: ManualClock) -> StoryFactory:
    """Build a canonical `Story` with sensible defaults."""
    counter = {"n": 0}

    def factory(
        *,
        platform: str = "reddit",
        source_id: str | None = None,
        title: str = "I finally confronted my roommate about the rent",
        body: str | None = None,
        canonical_url: str | None = None,
        created_at: datetime | None = None,
        discovered_at: datetime | None = None,
        score: int | None = 1200,
        comments: int | None = 240,
        quality_key: str = "TrueOffMyChest",
        metadata: Mapping[str, Any] | None = None,
    ) -> Story:
        counter["n"] += 1
        index = counter["n"]
        sid = source_id or f"t3_test{index:04d}"
        text = body if body is not None else _default_body(index)
        extra = {
            QUALITY_KEY: quality_key,
            RAW_FORMAT_KEY: "markdown",
            "subreddit": quality_key,
        }
        if metadata:
            extra.update(metadata)
        return build_story(
            platform=platform,
            source_id=sid,
            canonical_url=canonical_url
            or f"https://www.reddit.com/r/{quality_key}/comments/{sid}/",
            title=title,
            raw_content=text,
            normalized_content=text,
            created_at=created_at or datetime(2026, 7, 31, 12, 0, tzinfo=UTC),
            discovered_at=discovered_at or clock.now(),
            engagement=Engagement(score=score, comments=comments),
            metadata=extra,
            author=f"user_{index}",
        )

    return factory


def _default_body(index: int) -> str:
    """A body long enough to fingerprint and score meaningfully."""
    return (
        f"My roommate stopped paying rent about four months ago, and I let it slide because "
        f"they said work had dried up. I covered the shortfall out of my savings and told "
        f"myself it was temporary.\n\n"
        f"Then last week I came home early and found a brand new console in the living room. "
        f'I asked where the money came from and they said "it was a gift" without looking up. '
        f"I was furious. We argued for an hour and I said things I regret.\n\n"
        f"Eventually they admitted they had been spending the rent money for months. I told "
        f"them they had until the end of the month to move out. Now their family is calling "
        f"me every day telling me I ruined everything. Story variant {index}."
    )


def load_fixture(name: str) -> Any:
    """Load a recorded API payload from `tests/fixtures`."""
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))
