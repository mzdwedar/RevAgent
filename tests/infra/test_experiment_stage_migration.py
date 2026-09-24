"""`0011`: runs on the retired `experiment` stage move to `draft` (T22).

A run left on a stage no tool is declared for is shown nothing, and a turn shown
nothing looks exactly like a turn with nothing to do. So the remap is a migration, not
something a later deploy is trusted to remember.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from agentstack.storage import migrate

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


@pytest.fixture
def db(db: ConnectionPool) -> Iterator[ConnectionPool]:
    """The shared fixture resets `public`. The real migrations also create `audit` and
    `langgraph`, which would survive into the next test - here or elsewhere in the
    suite - and make it fail on a schema that already exists rather than on what it is
    testing. So they are cleared on the way in and on the way out."""
    _drop_side_schemas(db)
    yield db
    _drop_side_schemas(db)


def _drop_side_schemas(db: ConnectionPool) -> None:
    with db.connection() as conn:
        conn.execute("DROP SCHEMA IF EXISTS audit CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")


def _run_on(db: ConnectionPool, stage: str) -> None:
    with db.connection() as conn:
        conn.execute(
            "INSERT INTO sessions (session_id, user_id, tenant, created_at)"
            " VALUES ('s-1', 'u-1', 't', now()) ON CONFLICT DO NOTHING"
        )
        conn.execute(
            "INSERT INTO runs (run_id, session_id, tenant, acting_user, stage, channel)"
            " VALUES ('r-1', 's-1', 't', 'u-1', %s, 'test')",
            (stage,),
        )


def _stage(db: ConnectionPool) -> str:
    with db.connection() as conn:
        row = conn.execute("SELECT stage FROM runs WHERE run_id = 'r-1'").fetchone()
    assert row is not None
    return str(row[0])


def test_a_run_on_the_retired_stage_moves_to_draft(db: ConnectionPool) -> None:
    migrate.apply(db, MIGRATIONS)
    migrate.rollback_to(db, MIGRATIONS, version=10)
    _run_on(db, "experiment")

    migrate.apply(db, MIGRATIONS)

    assert _stage(db) == "draft"


def test_rolling_back_returns_experiment_stages_to_the_one_the_old_code_knows(
    db: ConnectionPool,
) -> None:
    migrate.apply(db, MIGRATIONS)
    _run_on(db, "rollout")

    migrate.rollback_to(db, MIGRATIONS, version=10)

    assert _stage(db) == "experiment"


def test_other_stages_are_left_alone(db: ConnectionPool) -> None:
    migrate.apply(db, MIGRATIONS)
    migrate.rollback_to(db, MIGRATIONS, version=10)
    _run_on(db, "default")

    migrate.apply(db, MIGRATIONS)

    assert _stage(db) == "default"
