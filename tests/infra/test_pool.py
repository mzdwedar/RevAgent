"""The connection factory: one pool, sized on purpose, closed on exit."""

from __future__ import annotations

import pytest
from psycopg_pool import ConnectionPool

from agentstack.storage import pool as pool_module


def test_a_pooled_connection_round_trips(db: ConnectionPool) -> None:
    with db.connection() as conn:
        assert conn.execute("SELECT 1").fetchone() == (1,)


def test_the_pool_is_sized_from_arguments_not_from_the_default(db: ConnectionPool) -> None:
    assert (db.min_size, db.max_size) == (1, 4)


def test_the_default_size_is_a_named_constant_not_a_literal() -> None:
    """T21 tunes concurrency. It needs one number to change, in one place."""
    assert pool_module.DEFAULT_MIN_SIZE >= 1
    assert pool_module.DEFAULT_MAX_SIZE >= pool_module.DEFAULT_MIN_SIZE


def test_the_environment_overrides_the_dev_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "postgresql://elsewhere/other")
    assert pool_module.database_url() == "postgresql://elsewhere/other"


def test_without_the_environment_it_falls_back_to_the_dev_substrate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert pool_module.database_url() == pool_module.DEV_DATABASE_URL
    assert ":5433/" in pool_module.DEV_DATABASE_URL


def test_closing_the_pool_is_what_the_context_manager_does(test_database: str) -> None:
    with pool_module.open_pool(test_database, min_size=1, max_size=2) as open_pool:
        assert not open_pool.closed
    assert open_pool.closed


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://u:secret@host/db",
        "user=u password=secret host=host dbname=db",
    ],
)
def test_redaction_masks_the_password_in_either_connection_form(url: str) -> None:
    """Both forms are valid DATABASE_URL values, and both end up in CI logs."""
    out = pool_module.redacted(url)

    assert "secret" not in out
    assert "***" in out
    assert "u" in out  # the user survives; it is the password that does not


@pytest.mark.parametrize(
    "url",
    ["postgresql://host/db", "host=host dbname=db"],
)
def test_redaction_leaves_a_passwordless_url_alone(url: str) -> None:
    assert "***" not in pool_module.redacted(url)


def test_an_unparseable_connection_string_is_described_not_echoed() -> None:
    assert pool_module.redacted("=====") == "<unparseable connection string>"
