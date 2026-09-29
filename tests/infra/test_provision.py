"""Rebuilding a database is destructive, so the guard on it is the part that matters."""

from __future__ import annotations

import subprocess
import sys
from typing import Any

import psycopg
import pytest

from agentstack.storage import provision
from agentstack.storage.database import Database
from agentstack.storage.pool import open_pool
from tests.conftest import admin_url, rebuild


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
    url = rebuild("blank_test")
    with open_pool(url, min_size=1, max_size=1) as pool:
        provision.truncate_all(Database(pool=pool))  # no migrations applied, no tables


# --- one run, one set of databases ---
#
# Every run on a machine used to rebuild the same `agentstack_app_test`, and each
# rebuild dropped it under every other run: whole suites failed on rows that vanished
# mid-test. A run now gets names of its own and drops them when it ends.


def test_a_scoped_name_keeps_its_disposable_suffix_last() -> None:
    scoped = provision.run_scoped("agentstack_app_test")
    assert scoped == f"agentstack_app_{provision.RUN_TOKEN}_test"
    assert provision.run_scoped("agentstack_evals").endswith(f"_{provision.RUN_TOKEN}_evals")


def test_one_process_asks_for_a_name_and_always_gets_the_same_one() -> None:
    assert provision.run_scoped("agentstack_app_test") == provision.run_scoped(
        "agentstack_app_test"
    )


def test_two_processes_never_share_a_name() -> None:
    """The point of it: another session's run is another process."""
    ask = "from agentstack.storage.provision import run_scoped; print(run_scoped('x_test'))"
    names = {
        subprocess.run(
            [sys.executable, "-c", ask], capture_output=True, text=True, check=True
        ).stdout.strip()
        for _ in range(2)
    } | {provision.run_scoped("x_test")}
    assert len(names) == 3


@pytest.mark.parametrize(
    "action",
    [
        provision.run_scoped,
        lambda name: provision.drop_database("postgresql://nobody@localhost:59999/x", name),
    ],
)
def test_scoping_and_dropping_refuse_a_name_that_is_not_disposable(action: Any) -> None:
    """Refused on the name alone, before any connection: the address goes nowhere."""
    with pytest.raises(provision.NotDisposable):
        action("agentstack")


def test_a_dropped_database_is_gone() -> None:
    url = rebuild("dropped_test")
    provision.drop_database(admin_url(), provision.run_scoped("dropped_test"))
    with pytest.raises(psycopg.OperationalError, match="does not exist"):
        psycopg.connect(url, connect_timeout=5).close()


def test_this_suite_runs_on_a_database_of_its_own(app_database_url: str) -> None:
    assert f"dbname={provision.run_scoped('agentstack_app_test')}" in app_database_url
    assert "dbname=agentstack_app_test " not in f"{app_database_url} "
