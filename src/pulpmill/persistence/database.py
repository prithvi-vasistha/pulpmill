"""SQLite connection management.

Why SQLite: this is a single-machine, single-writer pipeline. A server database
would add an always-on process, a socket, a backup story and a failure mode, and
buy nothing at this scale. WAL mode already gives concurrent readers, which is
all `pulpmill status` needs while a run is writing.

Everything that mutates goes through `transaction()`. There is no autocommit
path, so a crash mid-stage cannot leave a half-written story.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import TracebackType
from typing import Any

from pulpmill.config.models import DatabaseConfig
from pulpmill.domain.errors import PersistenceError
from pulpmill.infrastructure.logging import get_logger

_log = get_logger("persistence.database")


class Database:
    """A configured SQLite connection with transaction helpers.

    The connection is created with `check_same_thread=False` and guarded by a
    reentrant lock, so bounded worker concurrency can share one instance safely.
    """

    def __init__(self, path: Path, config: DatabaseConfig) -> None:
        self._path = path
        self._config = config
        self._lock = threading.RLock()
        self._connection: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    def connect(self) -> sqlite3.Connection:
        """Open the connection if needed and apply pragmas. Idempotent."""
        with self._lock:
            if self._connection is not None:
                return self._connection

            if self._path.parent and str(self._path.parent) not in {"", "."}:
                self._path.parent.mkdir(parents=True, exist_ok=True)

            try:
                connection = sqlite3.connect(
                    self._path,
                    # Manual transaction control: no implicit BEGIN/COMMIT.
                    isolation_level=None,
                    check_same_thread=False,
                    timeout=self._config.busy_timeout_ms / 1000,
                )
            except sqlite3.Error as exc:
                raise PersistenceError("could not open database", path=str(self._path)) from exc

            connection.row_factory = sqlite3.Row
            cursor = connection.cursor()
            try:
                cursor.execute(f"PRAGMA journal_mode = {self._config.journal_mode}")
                cursor.execute(f"PRAGMA synchronous = {self._config.synchronous}")
                cursor.execute(f"PRAGMA busy_timeout = {self._config.busy_timeout_ms}")
                cursor.execute(
                    f"PRAGMA foreign_keys = {'ON' if self._config.foreign_keys else 'OFF'}"
                )
                # Bounded page cache (~8 MiB) -- this shares a 16 GB laptop with
                # everything else and must not grow without limit.
                cursor.execute("PRAGMA cache_size = -8000")
                cursor.execute("PRAGMA temp_store = MEMORY")
            finally:
                cursor.close()

            self._connection = connection
            _log.debug(
                "database_opened",
                path=str(self._path),
                journal_mode=self._config.journal_mode,
            )
            return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Run a block inside one transaction.

        Commits on clean exit, rolls back on any exception. Reentrant: a nested
        call joins the outer transaction rather than starting a second one, so
        repositories compose without either knowing about the other.
        """
        connection = self.connect()
        with self._lock:
            if connection.in_transaction:
                yield connection
                return
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
            except BaseException:
                connection.rollback()
                raise
            connection.commit()

    def execute(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> sqlite3.Cursor:
        """Run a read query. Writes belong in `transaction()`."""
        return self.connect().execute(sql, params)

    def query_one(
        self, sql: str, params: Sequence[Any] | dict[str, Any] = ()
    ) -> sqlite3.Row | None:
        cursor = self.execute(sql, params)
        try:
            row: sqlite3.Row | None = cursor.fetchone()
            return row
        finally:
            cursor.close()

    def query_all(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> list[sqlite3.Row]:
        cursor = self.execute(sql, params)
        try:
            return cursor.fetchall()
        finally:
            cursor.close()

    def query_scalar(self, sql: str, params: Sequence[Any] | dict[str, Any] = ()) -> Any:
        row = self.query_one(sql, params)
        return None if row is None else row[0]

    def table_names(self) -> list[str]:
        rows = self.query_all("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        return [str(row["name"]) for row in rows]

    def close(self) -> None:
        with self._lock:
            if self._connection is not None:
                self._connection.close()
                self._connection = None
                _log.debug("database_closed", path=str(self._path))

    def __enter__(self) -> Database:
        self.connect()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
