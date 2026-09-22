"""The demo is documentation that can rot, so it runs in CI like everything else."""

from __future__ import annotations

import pytest

from agentstack.interfaces.cli import main, walk_through
from agentstack.interfaces.wiring import build_stack
from agentstack.storage.database import Database


def test_the_walkthrough_runs_and_commits_exactly_one_refund(
    app_database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    """Driven against the test database, not `main()`.

    `main()` opens a pool on `DATABASE_URL`, which in a developer's shell is the dev
    database. A test that writes real sessions into it to prove a demo still runs is
    not a trade worth making.
    """
    walk_through(build_stack(app_database))
    out = capsys.readouterr().out
    assert "awaiting_approval" in out
    assert "surface commits : 1" in out
    assert "missing  : none of the required spans" in out


def test_main_opens_its_own_pool_from_the_environment(
    app_database_url: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Covers the wiring `walk_through` deliberately does not: pool in, pool out.

    Pointed at the test database, because the whole reason the test above calls
    `walk_through` is that `main()` would otherwise write to the dev one.
    """
    monkeypatch.setenv("DATABASE_URL", app_database_url)

    main()

    assert "surface commits : 1" in capsys.readouterr().out
