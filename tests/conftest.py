"""The substrate the whole suite runs on.

From T1 onward these tests talk to a real Postgres and fail loudly when it is absent.
`tests/infra` owns a database of its own because it drops and rebuilds schemas to test
the migrator; everything else shares the one below, migrated once per session.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from agentstack.storage import migrate
from agentstack.storage.database import Database
from agentstack.storage.pool import DEV_DATABASE_URL, open_pool
from agentstack.storage.provision import rebuild_database, truncate_all

MIGRATIONS = Path(__file__).resolve().parents[1] / "migrations"


def admin_url() -> str:
    return os.environ.get("DATABASE_URL") or DEV_DATABASE_URL


def rebuild(database: str) -> str:
    return rebuild_database(admin_url(), database)


@pytest.fixture(scope="session")
def app_database_url() -> str:
    return rebuild("agentstack_app_test")


@pytest.fixture(scope="session")
def _migrated(app_database_url: str) -> Iterator[Database]:
    """One pool and one migration run for the whole session."""
    with open_pool(app_database_url, min_size=1, max_size=8) as pool:
        migrate.apply(pool, MIGRATIONS)
        yield Database(pool=pool)


@pytest.fixture
def app_database(_migrated: Database) -> Database:
    """A migrated, empty database, per test.

    T2 shared this across the session on the reasoning that sessions are uuid-keyed
    and everything else hangs off a session id, so tests could not collide. That was
    wrong, and T4 is where it showed: an idempotency key is `refund:{tenant}:{charge}:
    {amount}`, deliberately stable across runs, so the first test to refund ch-7
    settles that key for every test after it. Emptying the tables is the fix; a
    per-run key would have traded a real safety property for test convenience.
    """
    truncate_all(_migrated)
    return _migrated
