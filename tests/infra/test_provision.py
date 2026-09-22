"""Rebuilding a database is destructive, so the guard on it is the part that matters."""

from __future__ import annotations

import pytest

from agentstack.storage import provision
from agentstack.storage.database import Database
from agentstack.storage.pool import open_pool
from tests.conftest import admin_url


def test_a_database_that_does_not_look_disposable_is_not_dropped() -> None:
    """A typo in DATABASE_URL must not be able to destroy the real one."""
    with pytest.raises(provision.NotDisposable, match="agentstack"):
        provision.rebuild_database(
            "postgresql://agent:agent@localhost:5433/agentstack", "agentstack"
        )


@pytest.mark.parametrize("name", ["agentstack_test", "agentstack_evals"])
def test_the_disposable_suffixes_are_allowed(name: str) -> None:
    assert name.endswith(provision.DISPOSABLE_SUFFIXES)


def test_the_guard_runs_before_any_connection_is_opened() -> None:
    """It refuses on the name alone, so an unreachable server is not what stops it."""
    with pytest.raises(provision.NotDisposable):
        provision.rebuild_database("postgresql://nobody@localhost:59999/x", "production")


def test_an_unreachable_substrate_says_how_to_bring_it_up() -> None:
    with pytest.raises(provision.SubstrateUnreachable) as caught:
        provision.rebuild_database(
            "postgresql://agent:hunter2@localhost:59999/agentstack", "scratch_test"
        )

    message = str(caught.value)
    assert provision.BRING_UP in message
    assert "hunter2" not in message, "the error must not leak the password"


def test_url_for_keeps_the_connection_details_and_changes_the_database() -> None:
    url = provision.url_for("postgresql://agent:agent@localhost:5433/agentstack", "other_test")

    assert "dbname=other_test" in url
    assert "port=5433" in url
    assert "user=agent" in url


def test_emptying_a_database_that_does_not_look_disposable_is_refused() -> None:
    """The connection is asked its own name; the caller is not taken at its word.

    Pointed at `postgres`, which holds none of this project's tables - so a regression
    in the guard shows up as a failing assertion rather than as a wiped database.
    """
    url = provision.url_for(admin_url(), "postgres")
    with (
        open_pool(url, min_size=1, max_size=1) as pool,
        pytest.raises(provision.NotDisposable, match="postgres"),
    ):
        provision.truncate_all(Database(pool=pool))


def test_emptying_a_database_with_no_tables_yet_does_nothing() -> None:
    url = provision.rebuild_database(admin_url(), "blank_test")
    with open_pool(url, min_size=1, max_size=1) as pool:
        provision.truncate_all(Database(pool=pool))  # no migrations applied, no tables
