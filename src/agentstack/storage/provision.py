"""Creating and destroying disposable databases, for dev and test substrates.

This exists because the alternative was the same `DROP DATABASE` written twice, in
the test conftest and in the eval runner, with no guard on either. It lives here
because it is driver work, and the driver lives here.

It will not drop a database whose name is not visibly disposable. A `DATABASE_URL`
with a typo in it should not be able to destroy the one holding real runs, and the
name is the only signal available at the point of the call.
"""

from __future__ import annotations

import psycopg
from psycopg import OperationalError
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from agentstack.storage.pool import redacted

BRING_UP = "bash scripts/dev_up.sh"

# A database is disposable when its name says so. Nothing else here is safe to infer.
DISPOSABLE_SUFFIXES = ("_test", "_evals")


class NotDisposable(RuntimeError):
    """Asked to destroy a database that does not look like a throwaway."""


class SubstrateUnreachable(RuntimeError):
    """Postgres is not running. Tests do not skip on this; they say so."""


def url_for(admin_url: str, database: str) -> str:
    """The same connection details, pointed at a different database."""
    parts = conninfo_to_dict(admin_url)
    parts["dbname"] = database
    return make_conninfo("", **parts)


def rebuild_database(admin_url: str, database: str) -> str:
    """Drop `database` if it exists and create it empty. Returns its URL."""
    if not database.endswith(DISPOSABLE_SUFFIXES):
        raise NotDisposable(
            f"{database!r} does not end in one of {DISPOSABLE_SUFFIXES}; refusing to drop it. "
            "Rebuilding is for throwaway substrates, and the name is the only thing "
            "distinguishing one from the database holding real runs."
        )
    try:
        admin = psycopg.connect(admin_url, autocommit=True, connect_timeout=5)
    except OperationalError as exc:
        raise SubstrateUnreachable(
            f"Postgres is not reachable at {redacted(admin_url)}.\n"
            f"Bring the substrate up:  {BRING_UP}\n"
            f"Driver said: {exc}"
        ) from exc
    with admin:
        admin.execute(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)')
        admin.execute(f'CREATE DATABASE "{database}"')
    return url_for(admin_url, database)
