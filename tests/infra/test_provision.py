"""Rebuilding a database is destructive, so the guard on it is the part that matters."""

from __future__ import annotations

import pytest

from agentstack.storage import provision


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
