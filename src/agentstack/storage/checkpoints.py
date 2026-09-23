"""The LangGraph checkpoint store, kept in a schema of its own.

`PostgresSaver` has no schema parameter: it creates `checkpoints`, `checkpoint_blobs`,
`checkpoint_writes` and `checkpoint_migrations` wherever the connection's search_path
points. Left at the default that is `public`, beside this system's own tables and its
own migration ledger - two version histories in one namespace, each unaware of the
other, and `migrate down --to 0` leaving four tables nobody can account for.

So the saver gets a pool of its own whose search_path is `langgraph` and nothing else.
The schema is created by `migrations/0004`; the tables in it are the library's to make
and to migrate.

This is also why the saver is built here rather than in `agentstack.runtime`: it holds
a database handle, and only this package does that (contract 5).
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.postgres import PostgresSaver
from psycopg import Connection
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

from agentstack.storage.pool import DEFAULT_TIMEOUT_SECONDS, database_url

CHECKPOINT_SCHEMA = "langgraph"


class CheckpointSchemaMissing(RuntimeError):
    """The checkpoint schema has not been migrated into existence yet."""


def checkpoint_url(url: str | None = None, *, schema: str = CHECKPOINT_SCHEMA) -> str:
    """The same database, with the session pinned to the checkpoint schema.

    `-c search_path=...` rather than a qualified table name, because the qualifying is
    not ours to do: the library writes the table names and we do not get to edit them.
    """
    parts = conninfo_to_dict(url or database_url())
    existing = str(parts.get("options") or "").strip()
    parts["options"] = f"{existing} -c search_path={schema}".strip()
    return make_conninfo("", **parts)


def open_checkpointer(
    url: str | None = None, *, min_size: int = 1, max_size: int = 4
) -> tuple[ConnectionPool[Connection[DictRow]], PostgresSaver]:
    """Open a pool for checkpoints and prepare the saver's own tables.

    Returns the pool as well, because whoever opens it has to close it. Hiding that
    behind a factory that returns only the saver is how a pool outlives the thing that
    needed it.
    """
    pool: ConnectionPool[Connection[DictRow]] = ConnectionPool(
        conninfo=checkpoint_url(url),
        connection_class=Connection[DictRow],
        min_size=min_size,
        max_size=max_size,
        timeout=DEFAULT_TIMEOUT_SECONDS,
        # autocommit: `setup()` issues CREATE INDEX CONCURRENTLY, which Postgres
        # refuses inside a transaction block. It also means a checkpoint write has
        # landed when the call returns, which is the whole point of durability="sync" -
        # a write still inside an open transaction when the process dies is not a write.
        #
        # prepare_threshold=0: pooled connections are handed round, and server-side
        # prepared statements tied to one of them are a well-known way to get
        # "prepared statement does not exist" under a connection pooler.
        #
        # row_factory: the saver reads its rows as mappings. This is its contract,
        # not a preference - a tuple-row pool type-checks as a pool and fails at the
        # first read.
        kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        open=True,
    )
    _require_schema(pool)
    saver = PostgresSaver(pool)
    # Idempotent: the library keeps its own ledger and applies only what is missing.
    saver.setup()
    return pool, saver


def _require_schema(pool: ConnectionPool[Connection[DictRow]]) -> None:
    """Fail with the command that fixes it, rather than with Postgres' version.

    With search_path pointing at a schema that does not exist yet, `setup()` reports
    "no schema has been selected to create in", which names neither the schema nor the
    migration that creates it. This ordering is easy to get wrong - the checkpointer
    looks like infrastructure that comes before migrations, and it is the one piece
    that comes after.
    """
    with pool.connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM information_schema.schemata WHERE schema_name = %s",
            (CHECKPOINT_SCHEMA,),
        ).fetchone()
    if row is None:
        raise CheckpointSchemaMissing(
            f"the {CHECKPOINT_SCHEMA!r} schema does not exist, so the checkpointer has "
            "nowhere to put its tables. Migrations create it: run "
            "`uv run agentstack-migrate up` (or scripts/dev_up.sh) before opening a "
            "checkpointer."
        )


def in_memory_checkpointer() -> Any:
    """A checkpointer that does not survive the process.

    Exists so a caller can be explicit about wanting that - a demo, a scratch script -
    rather than getting it by leaving an argument off.
    """
    from langgraph.checkpoint.memory import InMemorySaver

    return InMemorySaver()
