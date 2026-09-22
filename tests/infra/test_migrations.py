"""The schema is a versioned artifact, not whatever the last developer ran.

Every store in T2-T4 lands on this. If the migrator is wrong, the durability those
tasks claim is a claim about a schema nobody can reproduce.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from agentstack.storage import migrate
from tests.infra.conftest import table_names, write_migration


def test_apply_runs_every_migration_in_order(db: ConnectionPool, migrations: Path) -> None:
    applied = migrate.apply(db, migrations)

    assert [m.version for m in applied] == [1, 2]
    assert "widgets" in table_names(db)
    assert [m.version for m in migrate.applied(db)] == [1, 2]


def test_down_then_up_again_reaches_the_same_schema(db: ConnectionPool, migrations: Path) -> None:
    migrate.apply(db, migrations)
    before = table_names(db)

    migrate.rollback_to(db, migrations, version=0)
    assert "widgets" not in table_names(db)
    assert migrate.applied(db) == ()

    migrate.apply(db, migrations)
    assert table_names(db) == before


def test_rollback_stops_at_the_named_version(db: ConnectionPool, migrations: Path) -> None:
    migrate.apply(db, migrations)

    rolled_back = migrate.rollback_to(db, migrations, version=1)

    assert [m.version for m in rolled_back] == [2]
    assert "widgets" in table_names(db)
    assert [m.version for m in migrate.applied(db)] == [1]


def test_applying_an_already_migrated_database_changes_nothing(
    db: ConnectionPool, migrations: Path
) -> None:
    migrate.apply(db, migrations)

    assert migrate.apply(db, migrations) == ()
    assert migrate.pending(db, migrations) == ()


def test_an_edited_migration_is_refused_rather_than_re_run(
    db: ConnectionPool, migrations: Path
) -> None:
    """An edited migration means two databases disagree about what 0001 is.

    Same failure class as T19's incompatible checkpoint: the fix is to notice, not to
    guess which version the database in front of you has.
    """
    migrate.apply(db, migrations)
    (migrations / "0001_widgets.up.sql").write_text("CREATE TABLE widgets (id bigint);\n")

    with pytest.raises(migrate.ChecksumMismatch) as caught:
        migrate.apply(db, migrations)

    assert "0001_widgets" in str(caught.value)


def test_a_migration_recorded_but_missing_from_disk_is_refused(
    db: ConnectionPool, migrations: Path
) -> None:
    """A revert dropped the file but the database still has the table.

    The newest migration is removed, not the oldest: deleting 0001 leaves a version
    gap, which `discover` rejects first, and the history check would never run.
    """
    migrate.apply(db, migrations)
    (migrations / "0002_widget_label.up.sql").unlink()
    (migrations / "0002_widget_label.down.sql").unlink()

    with pytest.raises(migrate.MigrationError, match="not in the migration set"):
        migrate.apply(db, migrations)


def test_a_missing_down_file_blocks_rollback_instead_of_half_doing_it(
    db: ConnectionPool, tmp_path: Path
) -> None:
    directory = tmp_path / "one-way"
    directory.mkdir()
    write_migration(directory, 1, "gadgets", up="CREATE TABLE gadgets (id int)", down=None)
    migrate.apply(db, directory)

    with pytest.raises(migrate.MissingDownMigration, match="0001_gadgets"):
        migrate.rollback_to(db, directory, version=0)

    assert "gadgets" in table_names(db)


def test_a_gap_in_the_version_sequence_is_refused(db: ConnectionPool, tmp_path: Path) -> None:
    directory = tmp_path / "gappy"
    directory.mkdir()
    write_migration(directory, 1, "a", up="CREATE TABLE a (id int)", down="DROP TABLE a")
    write_migration(directory, 3, "c", up="CREATE TABLE c (id int)", down="DROP TABLE c")

    with pytest.raises(migrate.DiscontinuousMigrations, match="2"):
        migrate.apply(db, directory)


def test_a_failing_migration_leaves_no_trace_of_itself(db: ConnectionPool, tmp_path: Path) -> None:
    """Each migration is one transaction: it lands whole or not at all."""
    directory = tmp_path / "broken"
    directory.mkdir()
    write_migration(directory, 1, "fine", up="CREATE TABLE fine (id int)", down="DROP TABLE fine")
    write_migration(directory, 2, "broken", up="CREATE TABLE fine (id int)", down="SELECT 1")

    with pytest.raises(Exception, match="already exists"):
        migrate.apply(db, directory)

    assert [m.version for m in migrate.applied(db)] == [1]
    assert "fine" in table_names(db)


def test_two_migrators_do_not_race(db: ConnectionPool, tmp_path: Path) -> None:
    """Two processes deploying at once must not both run 0001.

    Without the advisory lock the loser raises "relation already exists"; with it, the
    loser waits, sees the work done, and reports nothing to do.
    """
    directory = tmp_path / "slow"
    directory.mkdir()
    write_migration(
        directory,
        1,
        "slow",
        up="SELECT pg_sleep(0.4); CREATE TABLE slow (id int)",
        down="DROP TABLE slow",
    )

    results: list[object] = []
    barrier = threading.Barrier(2)

    def run() -> None:
        barrier.wait()
        try:
            results.append(migrate.apply(db, directory))
        except Exception as exc:  # recorded, then asserted on below
            results.append(exc)

    threads = [threading.Thread(target=run) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert not any(isinstance(r, Exception) for r in results), results
    assert sorted(len(r) for r in results if isinstance(r, tuple)) == [0, 1]
    assert [m.version for m in migrate.applied(db)] == [1]


def test_the_repositorys_own_migration_directory_is_well_formed(db: ConnectionPool) -> None:
    """Guards `migrations/` itself as T2 onwards fill it: no gaps, no one-way steps."""
    directory = Path(__file__).resolve().parents[2] / "migrations"

    discovered = migrate.discover(directory)

    assert [m.version for m in discovered] == list(range(1, len(discovered) + 1))
    assert [m.name for m in discovered if m.down_path is None] == []
    migrate.apply(db, directory)


def test_a_file_that_is_not_a_migration_is_refused_rather_than_ignored(tmp_path: Path) -> None:
    """Silently skipping it is how `0004_fix.sql` never runs and nobody finds out."""
    directory = tmp_path / "stray"
    directory.mkdir()
    write_migration(directory, 1, "a", up="CREATE TABLE a (id int)", down="DROP TABLE a")
    (directory / "0002_missing_direction.sql").write_text("SELECT 1;\n")

    with pytest.raises(migrate.MigrationError, match="not a migration filename"):
        migrate.discover(directory)


def test_one_version_with_two_names_is_refused(tmp_path: Path) -> None:
    """Two branches both took 0003. Picking one alphabetically is not a merge."""
    directory = tmp_path / "collision"
    directory.mkdir()
    write_migration(
        directory, 1, "alpha", up="CREATE TABLE alpha (id int)", down="DROP TABLE alpha"
    )
    (directory / "0001_beta.up.sql").write_text("CREATE TABLE beta (id int);\n")

    with pytest.raises(migrate.MigrationError, match="two names"):
        migrate.discover(directory)


def test_a_down_file_with_no_up_file_is_refused(tmp_path: Path) -> None:
    directory = tmp_path / "orphan"
    directory.mkdir()
    (directory / "0001_orphan.down.sql").write_text("DROP TABLE nothing;\n")

    with pytest.raises(migrate.MigrationError, match="no up file"):
        migrate.discover(directory)


def test_rolling_back_a_migration_deleted_from_disk_is_refused(
    db: ConnectionPool, migrations: Path
) -> None:
    """The down SQL is gone, so the only honest answer is to stop and say so."""
    migrate.apply(db, migrations)
    for path in migrations.glob("0002_*"):
        path.unlink()

    with pytest.raises(migrate.MigrationError, match="0002_widget_label"):
        migrate.rollback_to(db, migrations, version=1)
