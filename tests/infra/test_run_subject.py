"""`0013`: a run records the experiment it is about (H3).

The subject is what narrows an evaluation run's envelope, and a run is resumed in
whichever process picks it up. So it is a column, and it has to come back out of
Postgres exactly as it went in, or a resumed run is tenant-wide again.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from agentstack.runtime.run import RunStore, new_run
from agentstack.storage import migrate
from agentstack.storage.database import Database, IntegrityViolation
from agentstack.tools.experiments import EVALUATION_STAGE, ROLLOUT_STAGE

MIGRATIONS = Path(__file__).resolve().parents[2] / "migrations"


def _session(db: Database, session_id: str = "s-1") -> str:
    db.execute(
        "INSERT INTO sessions (session_id, user_id, tenant, created_at)"
        " VALUES (%s, 'u-1', 'acme', now()) ON CONFLICT DO NOTHING",
        (session_id,),
    )
    return session_id


def test_a_subject_survives_the_round_trip(app_database: Database) -> None:
    runs = RunStore(db=app_database)
    session = _session(app_database, "s-subject")
    bound = runs.ensure(
        new_run(
            session_id=session, tenant="acme", user="u-1", stage=EVALUATION_STAGE, subject="exp-7"
        )
    )
    unbound = runs.ensure(new_run(session_id=session, tenant="acme", user="u-1"))

    assert runs.get(bound.run_id) == bound
    got = runs.get(bound.run_id)
    assert got is not None and got.subject_resource() == "acme/experiments/exp-7"
    assert runs.get(unbound.run_id) == unbound
    assert unbound.subject is None


def test_a_rollout_run_may_be_bound_and_need_not_be(app_database: Database) -> None:
    runs = RunStore(db=app_database)
    session = _session(app_database, "s-rollout")
    bound = runs.ensure(
        new_run(session_id=session, tenant="acme", user="u", stage=ROLLOUT_STAGE, subject="e-1")
    )
    legacy = runs.ensure(new_run(session_id=session, tenant="acme", user="u", stage=ROLLOUT_STAGE))
    assert runs.get(bound.run_id) == bound
    assert runs.get(legacy.run_id) == legacy


@pytest.mark.parametrize("subject", ["", "  ", "exp-7/halt"])
def test_a_subject_is_one_identifier_in_code_and_in_the_table(
    app_database: Database, subject: str
) -> None:
    session = _session(app_database, "s-bad")
    with pytest.raises(ValueError, match="one experiment id"):
        new_run(session_id=session, tenant="acme", user="u", subject=subject)
    with pytest.raises(IntegrityViolation, match="run_subject_is_an_identifier"):
        app_database.execute(
            "INSERT INTO runs (run_id, session_id, tenant, acting_user, stage, channel, subject)"
            " VALUES ('r-bad', %s, 'acme', 'u', 'default', 't', %s)",
            (session, subject),
        )


def test_the_table_refuses_an_evaluation_run_with_no_subject(app_database: Database) -> None:
    """A path that skips the constructor still cannot write one."""
    session = _session(app_database, "s-eval")
    with pytest.raises(IntegrityViolation, match="evaluation_runs_name_their_subject"):
        app_database.execute(
            "INSERT INTO runs (run_id, session_id, tenant, acting_user, stage, channel)"
            " VALUES ('r-eval', %s, 'acme', 'u', 'evaluation', 't')",
            (session,),
        )


# --- the migration itself, from a database that predates it ---


@pytest.fixture
def pool(db: ConnectionPool) -> Iterator[ConnectionPool]:
    """As in the stage migration test: the real migrations create `audit` and
    `langgraph`, which the shared fixture does not reset."""
    _drop_side_schemas(db)
    yield db
    _drop_side_schemas(db)


def _drop_side_schemas(db: ConnectionPool) -> None:
    with db.connection() as conn:
        conn.execute("DROP SCHEMA IF EXISTS audit CASCADE")
        conn.execute("DROP SCHEMA IF EXISTS langgraph CASCADE")


def _legacy_evaluation_run(db: Database, run_id: str, *, cycle_for: str | None) -> None:
    session = _session(db)
    db.execute(
        "INSERT INTO runs (run_id, session_id, tenant, acting_user, stage, channel)"
        " VALUES (%s, %s, 'acme', 'u-1', 'evaluation', 'trigger')",
        (run_id, session),
    )
    if cycle_for is not None:
        db.execute(
            "INSERT INTO trigger_cycles (experiment_id, data_as_of, kind, outcome, run_id,"
            " settled_at) VALUES (%s, 'w', 'metric_movement', 'continue', %s, now())",
            (cycle_for, run_id),
        )


def test_the_migration_binds_existing_evaluation_runs_to_their_trigger(
    pool: ConnectionPool,
) -> None:
    """The trigger that woke a run named its experiment, and the cycle recorded both.
    That is the trusted source, so 0013 copies it; a run it cannot bind fails closed."""
    db = Database(pool=pool)
    migrate.apply(pool, MIGRATIONS)
    migrate.rollback_to(pool, MIGRATIONS, version=12)
    _legacy_evaluation_run(db, "r-woken", cycle_for="exp-7")
    _legacy_evaluation_run(db, "r-orphan", cycle_for=None)

    migrate.apply(pool, MIGRATIONS)

    runs = RunStore(db=db)
    woken = runs.get("r-woken")
    assert woken is not None and woken.subject == "exp-7"
    # Not resumed with tenant-wide halt authority: refused on load, loudly.
    with pytest.raises(ValueError, match="names no subject"):
        runs.get("r-orphan")


def test_rolling_back_drops_the_subject(pool: ConnectionPool) -> None:
    db = Database(pool=pool)
    migrate.apply(pool, MIGRATIONS)
    migrate.rollback_to(pool, MIGRATIONS, version=12)

    row = db.fetch_one(
        "SELECT count(*) FROM information_schema.columns"
        " WHERE table_name = 'runs' AND column_name = 'subject'"
    )
    assert row is not None and row[0] == 0
