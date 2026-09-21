"""The demo is documentation that can rot, so it runs in CI like everything else."""

from __future__ import annotations

import pytest

from agentstack.interfaces.cli import main


def test_the_walkthrough_runs_and_commits_exactly_one_refund(
    capsys: pytest.CaptureFixture[str],
) -> None:
    main()
    out = capsys.readouterr().out
    assert "awaiting_approval" in out
    assert "surface commits : 1" in out
    assert "missing  : none of the required spans" in out
