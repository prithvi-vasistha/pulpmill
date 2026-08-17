"""Schema migrations.

Numbered `.sql` files in `migrations/`, applied in order, each inside its own
transaction, each recorded with a checksum. Nothing else in the application
creates or alters a table -- there is no `CREATE TABLE IF NOT EXISTS` scattered
through the repositories.

The single exception is the `schema_migrations` bookkeeping table below, which
has to exist before any migration can be recorded.

Checksums are verified on every startup. If a migration file is edited after it
has been applied, that is reported as an error rather than silently ignored:
two machines would otherwise end up with different schemas from the same
version number.
"""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from pulpmill.domain.errors import MigrationError
from pulpmill.infrastructure.clock import utc_now
from pulpmill.infrastructure.logging import get_logger
from pulpmill.persistence.database import Database

_log = get_logger("persistence.migrations")

_FILENAME_PATTERN = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.sql$")

_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER NOT NULL PRIMARY KEY,
    name       TEXT    NOT NULL,
    checksum   TEXT    NOT NULL,
    applied_at TEXT    NOT NULL
)
"""


def split_sql_statements(script: str) -> list[str]:
    """Split a migration file into individual statements.

    Deliberately not `Connection.executescript`: that issues an implicit COMMIT
    before it runs, which would break the migration out of its transaction and
    let the DDL land without the matching `schema_migrations` row.

    Splitting uses `sqlite3.complete_statement` rather than naive semicolon
    splitting, so a semicolon inside a string literal or a comment does not cut
    a statement in half.
    """
    statements: list[str] = []
    buffer = ""
    for line in script.splitlines(keepends=True):
        buffer += line
        if sqlite3.complete_statement(buffer):
            statement = buffer.strip()
            if statement:
                statements.append(statement)
            buffer = ""

    remainder = buffer.strip()
    if remainder:
        meaningful = [
            line
            for line in remainder.splitlines()
            if line.strip() and not line.strip().startswith("--")
        ]
        if meaningful:
            raise MigrationError(
                "migration ends with an incomplete statement (missing semicolon?)",
                trailing=remainder[:120],
            )
    return statements


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sql: str

    @property
    def statements(self) -> list[str]:
        return split_sql_statements(self.sql)

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.sql.encode("utf-8")).hexdigest()

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


@dataclass(frozen=True, slots=True)
class MigrationStatus:
    applied: tuple[str, ...]
    pending: tuple[str, ...]

    @property
    def is_current(self) -> bool:
        return not self.pending


def default_migrations_dir(project_root: Path) -> Path:
    """Locate the migrations directory relative to the project root."""
    directory = project_root / "migrations"
    if directory.is_dir():
        return directory
    raise MigrationError("no migrations directory found", searched=str(directory))


def discover_migrations(directory: Path) -> list[Migration]:
    """Load and validate every migration file, ordered by version."""
    if not directory.is_dir():
        raise MigrationError("migrations directory does not exist", path=str(directory))

    migrations: list[Migration] = []
    seen: dict[int, Path] = {}

    for path in sorted(directory.glob("*.sql")):
        match = _FILENAME_PATTERN.match(path.name)
        if match is None:
            raise MigrationError(
                "migration filename must look like 0001_snake_case_name.sql",
                path=str(path),
            )
        version = int(match.group("version"))
        if version in seen:
            raise MigrationError(
                "duplicate migration version",
                version=version,
                first=str(seen[version]),
                second=str(path),
            )
        seen[version] = path
        migrations.append(
            Migration(
                version=version,
                name=match.group("name"),
                path=path,
                sql=path.read_text(encoding="utf-8"),
            )
        )

    migrations.sort(key=lambda migration: migration.version)
    for index, migration in enumerate(migrations, start=1):
        if migration.version != index:
            raise MigrationError(
                "migration versions must be consecutive starting at 0001",
                expected=index,
                found=migration.version,
                path=str(migration.path),
            )
    return migrations


class MigrationRunner:
    """Applies pending migrations and verifies already-applied ones."""

    def __init__(self, database: Database, directory: Path) -> None:
        self._database = database
        self._directory = directory

    def _ensure_bookkeeping(self) -> None:
        with self._database.transaction() as connection:
            connection.execute(_BOOTSTRAP_SQL)

    def _applied(self) -> dict[int, tuple[str, str]]:
        rows = self._database.query_all(
            "SELECT version, name, checksum FROM schema_migrations ORDER BY version"
        )
        return {int(row["version"]): (str(row["name"]), str(row["checksum"])) for row in rows}

    def status(self) -> MigrationStatus:
        self._ensure_bookkeeping()
        applied = self._applied()
        migrations = discover_migrations(self._directory)
        return MigrationStatus(
            applied=tuple(
                migration.label for migration in migrations if migration.version in applied
            ),
            pending=tuple(
                migration.label for migration in migrations if migration.version not in applied
            ),
        )

    def verify(self) -> None:
        """Confirm every applied migration still matches the file on disk."""
        self._ensure_bookkeeping()
        applied = self._applied()
        for migration in discover_migrations(self._directory):
            record = applied.get(migration.version)
            if record is None:
                continue
            _, checksum = record
            if checksum != migration.checksum:
                raise MigrationError(
                    "applied migration has been modified since it ran; "
                    "add a new migration instead of editing an old one",
                    migration=migration.label,
                    path=str(migration.path),
                )

    def upgrade(self) -> list[str]:
        """Apply every pending migration. Returns the labels applied.

        Idempotent: running it twice with nothing pending is a no-op.
        """
        self._ensure_bookkeeping()
        self.verify()

        applied = self._applied()
        performed: list[str] = []

        for migration in discover_migrations(self._directory):
            if migration.version in applied:
                continue
            # One transaction per migration: a failure leaves the database at
            # the last good version rather than half-upgraded.
            try:
                with self._database.transaction() as connection:
                    for statement in migration.statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations (version, name, checksum, applied_at) "
                        "VALUES (?, ?, ?, ?)",
                        (
                            migration.version,
                            migration.name,
                            migration.checksum,
                            utc_now().isoformat(),
                        ),
                    )
            except Exception as exc:
                raise MigrationError(
                    "migration failed",
                    migration=migration.label,
                    path=str(migration.path),
                    detail=str(exc),
                ) from exc

            performed.append(migration.label)
            _log.info("migration_applied", migration=migration.label)

        if not performed:
            _log.debug("migrations_up_to_date")
        return performed
