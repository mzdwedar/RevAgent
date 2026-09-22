"""A real Postgres, or a loud failure. Never a skip.

These tests are the only ones in the suite that need something outside the process.
The temptation is `skipif` - which would trip the floor in `CONSTRAINTS.md`, and the
tempting fix for that would be to loosen the floor. So they fail instead, with the one
command that makes them pass.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import psycopg
import pytest
from psycopg_pool import ConnectionPool

from agentstack.storage import pool as pool_module

BRING_UP = "bash scripts/dev_up.sh"
TEST_DATABASE = "agentstack_test"


def _admin_url() -> str:
    """The dev database, used only to create and drop the test database."""
    return os.environ.get("DATABASE_URL", pool_module.DEV_DATABASE_URL)


def _test_url() -> str:
    admin = psycopg.conninfo.conninfo_to_dict(_admin_url())
    admin["dbname"] = TEST_DATABASE
    return psycopg.conninfo.make_conninfo(**admin)


@pytest.fixture(scope="session")
def test_database() -> Iterator[str]:
    """A database of this suite's own, rebuilt from nothing once per session.

    Down-migration tests drop tables. Pointing them at the dev database would make
    running the suite destroy whatever was being worked on.
    """
    try:
        admin = psycopg.connect(_admin_url(), autocommit=True, connect_timeout=5)
    except psycopg.OperationalError as exc:
        raise RuntimeError(
            f"Postgres is not reachable at {pool_module.redacted(_admin_url())}.\n"
            f"These tests do not skip. Bring the substrate up:  {BRING_UP}\n"
            f"Driver said: {exc}"
        ) from exc
    with admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{TEST_DATABASE}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{TEST_DATABASE}"')
    yield _test_url()


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
