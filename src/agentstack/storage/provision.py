"""Creating and destroying disposable databases, for dev and test substrates.

This exists because the alternative was the same `DROP DATABASE` written twice, in
the test conftest and in the eval runner, with no guard on either. It lives here
because it is driver work, and the driver lives here.

It will not drop a database whose name is not visibly disposable. A `DATABASE_URL`
with a typo in it should not be able to destroy the one holding real runs, and the
name is the only signal available at the point of the call.
"""

from __future__ import annotations

import uuid

import psycopg
from psycopg import OperationalError
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agentstack.storage.database import Database
from agentstack.storage.pool import redacted

BRING_UP = "bash scripts/dev_up.sh"

# A database is disposable when its name says so. Nothing else here is safe to infer.
DISPOSABLE_SUFFIXES = ("_test", "_evals")

# This process's name on a Postgres other processes share (see `run_scoped`).
RUN_TOKEN = uuid.uuid4().hex[:12]


class NotDisposable(RuntimeError):
    """Asked to destroy a database that does not look like a throwaway."""


class SubstrateUnreachable(RuntimeError):
    """Postgres is not running. Tests do not skip on this; they say so."""


def url_for(admin_url: str, database: str) -> str:
    """The same connection details, pointed at a different database."""
    parts = conninfo_to_dict(admin_url)
    parts["dbname"] = database
    return make_conninfo("", **parts)


def run_scoped(database: str) -> str:
    """`database`, made this process's own: `agentstack_app_test` becomes
    `agentstack_app_<token>_test`.

    A fixed name is one database for every run on the machine, and a run that rebuilds
    it drops it under every other run using it: sessions sharing a dev Postgres saw
    their suites fail by the dozen on rows that vanished mid-test. One token per
    process, so every name a run asks for agrees with every other it asks for. The
    disposable suffix stays last, so the guard below still reads it.
    """
    _require_disposable(database)
    suffix = next(s for s in DISPOSABLE_SUFFIXES if database.endswith(s))
    return f"{database.removesuffix(suffix)}_{RUN_TOKEN}{suffix}"


def rebuild_database(admin_url: str, database: str) -> str:
    """Drop `database` if it exists and create it empty. Returns its URL."""
    _require_disposable(database)
    with _admin(admin_url) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{database}"')
    return url_for(admin_url, database)


def drop_database(admin_url: str, database: str) -> None:
    """Drop `database` if it exists: what a run does with its own scratch when it ends."""
    _require_disposable(database)
    with _admin(admin_url) as admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')


def _require_disposable(database: str) -> None:
    if not database.endswith(DISPOSABLE_SUFFIXES):
        raise NotDisposable(
            f"{database!r} does not end in one of {DISPOSABLE_SUFFIXES}; refusing to drop it. "
            "Rebuilding is for throwaway substrates, and the name is the only thing "
            "distinguishing one from the database holding real runs."
        )


def _admin(admin_url: str) -> psycopg.Connection[tuple[object, ...]]:
    try:
        return psycopg.connect(admin_url, autocommit=True, connect_timeout=5)
    except OperationalError as exc:
        raise SubstrateUnreachable(
            f"Postgres is not reachable at {redacted(admin_url)}.\n"
            f"Bring the substrate up:  {BRING_UP}\n"
            f"Driver said: {exc}"
        ) from exc


def truncate_all(db: Database) -> None:
    """Empty every table, keeping the schema and the migration ledger.

    For test and eval substrates. It asks the connection which database it is on and
    applies the same disposable-name rule as `rebuild_database`, because a helper
    whose whole job is to delete everything should not take the caller's word for it.

    Needed because not every key in this system is unique per run. An idempotency key
    is deliberately stable across runs - refunding charge ch-7 twice is the thing the
    ledger exists to stop, even from a different run - so tests sharing a database
    share those keys. The answer is a clean substrate per test, not a weaker key.
    """
    row = db.fetch_one("SELECT current_database()")
    name = str(row[0]) if row else "<unknown>"
    if not name.endswith(DISPOSABLE_SUFFIXES):
        raise NotDisposable(
            f"{name!r} does not end in one of {DISPOSABLE_SUFFIXES}; refusing to empty it."
        )
    tables = db.fetch_all(
        "SELECT schemaname, tablename FROM pg_tables"
        " WHERE schemaname IN ('public', 'audit') AND tablename <> 'schema_migrations'"
    )
    if not tables:
        return
    targets = ", ".join(f'"{schema}"."{table}"' for schema, table in tables)
    db.execute(f"TRUNCATE {targets} RESTART IDENTITY CASCADE")
