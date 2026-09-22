"""Versioned schema migrations, applied once and recorded.

Three properties this exists for, each of which is a way a deploy strands a database:

- **Contiguous versions.** A gap means a migration was lost in a merge, not skipped.
- **Immutable history.** An applied migration's checksum is recorded. Editing it
  afterwards is refused, because after that two databases disagree about what `0001`
  is, and nothing in the schema can tell you which one you are looking at. This is the
  same failure class as an incompatible checkpoint in T19 - noticed, never guessed.
- **One migrator at a time.** Two deploys racing both run `0001`; the loser gets
  "relation already exists" halfway through its own transaction. An advisory lock
  makes the loser wait and then find there is nothing to do.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from psycopg import Connection
from psycopg_pool import ConnectionPool

FILENAME = re.compile(r"^(?P<version>\d{4})_(?P<name>[a-z0-9_]+)\.(?P<direction>up|down)\.sql$")

# A fixed key so every migrator in every process contends on the same lock. Session
# scoped, not transaction scoped: each migration runs in its own transaction, and the
# lock has to outlive all of them.
LOCK_KEY = 0x41475354  # "AGST"

_CREATE_LEDGER = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    integer     PRIMARY KEY,
    name       text        NOT NULL,
    checksum   text        NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


class MigrationError(RuntimeError):
    """The migration set and the database disagree in a way only a human can settle."""


class ChecksumMismatch(MigrationError):
    """An already-applied migration was edited on disk."""


class MissingDownMigration(MigrationError):
    """Asked to roll back a step that was written as one-way."""


class DiscontinuousMigrations(MigrationError):
    """The version sequence has a hole in it."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    up_path: Path
    down_path: Path | None

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"

    @property
    def checksum(self) -> str:
        return hashlib.sha256(self.up_path.read_bytes()).hexdigest()


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    name: str
    checksum: str
    applied_at: datetime

    @property
    def label(self) -> str:
        return f"{self.version:04d}_{self.name}"


def discover(directory: Path) -> tuple[Migration, ...]:
    """Read the migration set off disk, ordered, with the sequence checked."""
    ups: dict[int, Path] = {}
    downs: dict[int, Path] = {}
    names: dict[int, str] = {}

    for path in sorted(directory.glob("*.sql")):
        match = FILENAME.match(path.name)
        if match is None:
            raise MigrationError(
                f"{path.name} is not a migration filename (NNNN_name.up.sql / .down.sql)"
            )
        version = int(match["version"])
        name = match["name"]
        if names.setdefault(version, name) != name:
            raise MigrationError(f"version {version:04d} has two names: {names[version]}, {name}")
        # No duplicate check on (version, direction): two files sharing a version but
        # not a name are caught above, and two sharing both cannot exist side by side.
        target = ups if match["direction"] == "up" else downs
        target[version] = path

    for version in sorted(downs):
        if version not in ups:
            raise MigrationError(f"version {version:04d} has a down file but no up file")

    for expected, version in enumerate(sorted(ups), start=1):
        if version != expected:
            raise DiscontinuousMigrations(
                f"migration {expected:04d} is missing; the next one on disk is {version:04d}"
            )

    return tuple(
        Migration(version=v, name=names[v], up_path=ups[v], down_path=downs.get(v))
        for v in sorted(ups)
    )


def applied(pool: ConnectionPool) -> tuple[AppliedMigration, ...]:
    """What this database records as already run, oldest first."""
    with pool.connection() as conn:
        _ensure_ledger(conn)
        return _applied(conn)


def pending(pool: ConnectionPool, directory: Path) -> tuple[Migration, ...]:
    """Discovered migrations this database has not run, with history verified first."""
    discovered = discover(directory)
    with pool.connection() as conn:
        _ensure_ledger(conn)
        done = _applied(conn)
    _verify_history(discovered, done)
    seen = {record.version for record in done}
    return tuple(m for m in discovered if m.version not in seen)


def apply(pool: ConnectionPool, directory: Path) -> tuple[Migration, ...]:
    """Run every pending migration, in order, one transaction each.

    Returns what it actually applied - empty when the database was already current.
    """
    discovered = discover(directory)
    with pool.connection() as conn, _exclusive(conn):
        _ensure_ledger(conn)
        done = _applied(conn)
        _verify_history(discovered, done)
        seen = {record.version for record in done}
        outstanding = [m for m in discovered if m.version not in seen]

        for migration in outstanding:
            with conn.transaction():
                conn.execute(migration.up_path.read_text())
                conn.execute(
                    "INSERT INTO schema_migrations (version, name, checksum) VALUES (%s, %s, %s)",
                    (migration.version, migration.name, migration.checksum),
                )
    return tuple(outstanding)


def rollback_to(pool: ConnectionPool, directory: Path, *, version: int) -> tuple[Migration, ...]:
    """Undo every applied migration above `version`, newest first.

    Refuses up front if any step in the range is one-way, rather than unwinding half
    of them and leaving the schema somewhere no version describes.
    """
    discovered = {m.version: m for m in discover(directory)}
    with pool.connection() as conn, _exclusive(conn):
        _ensure_ledger(conn)
        targets = [record for record in reversed(_applied(conn)) if record.version > version]

        steps: list[tuple[Migration, Path]] = []
        for record in targets:
            migration = discovered.get(record.version)
            if migration is None:
                raise MigrationError(f"{record.label} is recorded but missing from {directory}")
            if migration.down_path is None:
                raise MissingDownMigration(
                    f"{migration.label} has no down file; roll forward instead"
                )
            steps.append((migration, migration.down_path))

        for migration, down_path in steps:
            with conn.transaction():
                conn.execute(down_path.read_text())
                conn.execute(
                    "DELETE FROM schema_migrations WHERE version = %s", (migration.version,)
                )
    return tuple(m for m, _ in steps)


def _ensure_ledger(conn: Connection) -> None:
    conn.execute(_CREATE_LEDGER)
    conn.commit()


def _applied(conn: Connection) -> tuple[AppliedMigration, ...]:
    rows = conn.execute(
        "SELECT version, name, checksum, applied_at FROM schema_migrations ORDER BY version"
    ).fetchall()
    return tuple(AppliedMigration(*row) for row in rows)


def _verify_history(discovered: tuple[Migration, ...], done: tuple[AppliedMigration, ...]) -> None:
    """An applied migration must still exist on disk, unedited."""
    on_disk = {m.version: m for m in discovered}
    for record in done:
        migration = on_disk.get(record.version)
        if migration is None:
            raise MigrationError(
                f"{record.label} is recorded as applied but is not in the migration set"
            )
        if migration.checksum != record.checksum:
            raise ChecksumMismatch(
                f"{migration.label} was edited after it was applied; "
                "this database and a freshly migrated one no longer agree. Fix forward."
            )


class _exclusive:
    """Session-scoped advisory lock held across the whole migration run."""

    def __init__(self, conn: Connection) -> None:
        self._conn = conn

    def __enter__(self) -> None:
        self._conn.execute("SELECT pg_advisory_lock(%s)", (LOCK_KEY,))
        self._conn.commit()

    def __exit__(self, *_: object) -> None:
        self._conn.execute("SELECT pg_advisory_unlock(%s)", (LOCK_KEY,))
        self._conn.commit()
