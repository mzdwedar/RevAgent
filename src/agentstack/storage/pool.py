"""One pooled connection factory, sized on purpose.

A connection per operation is the default that survives a demo and dies under load;
an unbounded pool moves the failure into Postgres, which answers with "too many
clients". So the size is explicit, named, and owned by one module that T21 can tune.
"""

from __future__ import annotations

from psycopg import ProgrammingError
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg_pool import ConnectionPool

from agentstack.storage import secrets

# Dev only, and pointed at 5433 rather than 5432 so a laptop's existing Postgres is
# never mistaken for this one. Real credentials are the named iteration-2 debt.
DEV_DATABASE_URL = "postgresql://agent:agent@localhost:5433/agentstack"

# T21 verifies 100 concurrent runs. These are the numbers it tunes: a run holds a
# connection only across a step boundary, so the pool is far smaller than the run
# count, and `max_connections=200` in docker-compose.yml leaves room above it.
DEFAULT_MIN_SIZE = 2
DEFAULT_MAX_SIZE = 20

# How long a caller waits for a free connection before failing loudly. Blocking
# forever turns pool exhaustion into a hang, which is the harder thing to diagnose.
DEFAULT_TIMEOUT_SECONDS = 10.0


def database_url() -> str:
    """The configured database, or the dev substrate `scripts/dev_up.sh` brings up."""
    configured = secrets.read("DATABASE_URL")
    return configured.reveal() if configured else DEV_DATABASE_URL


def open_pool(
    url: str | None = None,
    *,
    min_size: int = DEFAULT_MIN_SIZE,
    max_size: int = DEFAULT_MAX_SIZE,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> ConnectionPool:
    """Open a pool against `url`, defaulting to `database_url()`.

    The returned pool is a context manager; closing it is the caller's job, and the
    tests take the `with` form so a leaked pool shows up as a hung suite rather than
    as a slow one.
    """
    return ConnectionPool(
        conninfo=url or database_url(),
        min_size=min_size,
        max_size=max_size,
        timeout=timeout,
        open=True,
    )


def redacted(url: str) -> str:
    """A connection string safe to print, in URL form or keyword form.

    Both forms are valid `DATABASE_URL` values and psycopg accepts either, so string
    surgery on "://" misses half of them - which is how a password reaches a CI log.
    Parsing with the driver's own parser is the only version that covers both.
    """
    try:
        parts = conninfo_to_dict(url)
    except ProgrammingError:
        return "<unparseable connection string>"
    if parts.get("password"):
        parts["password"] = "***"
    return make_conninfo("", **parts)
