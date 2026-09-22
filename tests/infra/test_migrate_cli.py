"""The operator entry point: it must report honestly and never print a password."""

from __future__ import annotations

from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from agentstack.interfaces import migrate_cli
from agentstack.storage import migrate


def _run(capsys: pytest.CaptureFixture[str], *argv: str, url: str) -> str:
    assert migrate_cli.main([*argv, "--url", url]) == 0
    return capsys.readouterr().out


def test_up_applies_and_reports_what_it_did(
    db: ConnectionPool, migrations: Path, test_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    out = _run(capsys, "up", "--directory", str(migrations), url=test_database)

    assert "applied 0001_widgets" in out
    assert "2 applied" in out
    assert [m.version for m in migrate.applied(db)] == [1, 2]


def test_status_separates_applied_from_pending(
    db: ConnectionPool, migrations: Path, test_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate.apply(db, migrations)
    migrate.rollback_to(db, migrations, version=1)

    out = _run(capsys, "status", "--directory", str(migrations), url=test_database)

    assert "applied  0001_widgets" in out
    assert "pending  0002_widget_label" in out
    assert "1 pending" in out


def test_down_needs_an_explicit_target(
    migrations: Path, test_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """`down` with no target is an unbounded destructive action. It is refused."""
    with pytest.raises(SystemExit):
        migrate_cli.main(["down", "--directory", str(migrations), "--url", test_database])

    assert "needs --to" in capsys.readouterr().err


def test_down_to_a_version_rolls_back_exactly_that_far(
    db: ConnectionPool, migrations: Path, test_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    migrate.apply(db, migrations)

    out = _run(capsys, "down", "--to", "1", "--directory", str(migrations), url=test_database)

    assert "rolled back 0002_widget_label" in out
    assert [m.version for m in migrate.applied(db)] == [1]


@pytest.mark.usefixtures("db")
def test_the_password_never_reaches_the_output(
    migrations: Path, test_database: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """This output lands in CI logs."""
    out = _run(capsys, "status", "--directory", str(migrations), url=test_database)

    assert "***" in out
    assert "agent:agent" not in out


def test_the_default_directory_is_the_repositorys_migrations() -> None:
    assert migrate_cli.MIGRATIONS.is_dir()
    assert migrate_cli.MIGRATIONS.name == "migrations"
