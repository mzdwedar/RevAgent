"""A database of this suite's own, because these tests drop schemas.

The migrator is what is under test here, so the fixtures below rebuild from nothing
per test. Everything else in the suite shares the migrated database in
`tests/conftest.py` instead.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agentstack.storage import pool as pool_module
from tests.conftest import rebuild

TEST_DATABASE = "agentstack_test"


@pytest.fixture(scope="session")
def test_database() -> Iterator[str]:
    """Rebuilt from nothing once per session.

    Down-migration tests drop tables. Pointing them at the dev database would make
    running the suite destroy whatever was being worked on.
    """
    yield rebuild(TEST_DATABASE)


@pytest.fixture
def db(test_database: str) -> Iterator[ConnectionPool]:
    """An empty schema and an open pool, per test."""
    with psycopg.connect(test_database, autocommit=True) as conn:
        conn.execute("DROP SCHEMA public CASCADE")
        conn.execute("CREATE SCHEMA public")
    with pool_module.open_pool(test_database, min_size=1, max_size=4) as open_pool:
        yield open_pool


@pytest.fixture
def migrations(tmp_path: Path) -> Path:
    """Two migrations that exercise ordering, rollback and a data change."""
    directory = tmp_path / "migrations"
    directory.mkdir()
    write_migration(
        directory,
        1,
        "widgets",
        up="CREATE TABLE widgets (id int PRIMARY KEY)",
        down="DROP TABLE widgets",
    )
    write_migration(
        directory,
        2,
        "widget_label",
        up="ALTER TABLE widgets ADD COLUMN label text",
        down="ALTER TABLE widgets DROP COLUMN label",
    )
    return directory


def write_migration(
    directory: Path, version: int, name: str, up: str, down: str | None = None
) -> None:
    """Write an up/down pair. `down=None` writes no down file, deliberately."""
    (directory / f"{version:04d}_{name}.up.sql").write_text(up + ";\n")
    if down is not None:
        (directory / f"{version:04d}_{name}.down.sql").write_text(down + ";\n")


def table_names(open_pool: ConnectionPool) -> set[str]:
    with open_pool.connection() as conn:
        rows = conn.execute(
            "SELECT tablename FROM pg_tables WHERE schemaname = 'public'"
        ).fetchall()
    return {row[0] for row in rows}
