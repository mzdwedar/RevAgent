"""The handle every layer above uses, with no driver type in its signature.

`lint-imports` contract 5 forbids `psycopg` outside this package. That rule would be
easy to satisfy dishonestly - move the stores down here and call it storage - which
would put layer 2's and layer 3's semantics inside layer 10. So the seam is this
object instead: `Database` takes SQL and parameters and gives back plain tuples, and
each store keeps its own SQL next to the invariants that SQL exists to hold.

Deliberately not here yet: a transaction scope. Nothing in T2 writes two rows that
must land together, and a `transaction()` that silently used a different pooled
connection per statement would be worse than none. It arrives with the first caller
that needs it, shaped by what that caller actually does.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from psycopg_pool import ConnectionPool


@dataclass(frozen=True, slots=True)
class Database:
    pool: ConnectionPool

    def execute(self, sql: str, params: Sequence[Any] = ()) -> None:
        with self.pool.connection() as conn:
            conn.execute(sql, params)

    def fetch_all(self, sql: str, params: Sequence[Any] = ()) -> list[tuple[Any, ...]]:
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchall()

    def fetch_one(self, sql: str, params: Sequence[Any] = ()) -> tuple[Any, ...] | None:
        with self.pool.connection() as conn:
            return conn.execute(sql, params).fetchone()
