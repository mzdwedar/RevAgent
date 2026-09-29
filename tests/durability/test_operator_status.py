"""T50: `operator status` puts a run's record beside its position, and neither for the other.

The record is Postgres: the run and the waits it parked. The position is Temporal: whether
the workflow runs, what it is waiting on, which activity is pending and how often it
has been tried. The retry policy has no attempt cap by design (Checkpoint I), so an
activity that fails forever is only ever *seen*, here, past `STUCK_AFTER_ATTEMPTS`.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

import pytest

from agentstack.interfaces import operator_cli
from agentstack.interfaces.wiring import Stack, deliver
from agentstack.prediction.churn import ChurnScores
from agentstack.runtime.temporal.client import position
from agentstack.runtime.temporal.contracts import RunProgress, workflow_id
from agentstack.runtime.temporal.retry import STUCK_AFTER_ATTEMPTS
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database
from tests.durability.test_approval_wait import PAYLOAD, propose_and_wait
from tests.durability.test_approval_wait import stack as stack  # the same fixture
from tests.fitness.test_trigger_to_candidate import RULE, StubScorer
from tests.temporal_support import (
    DraftingEngine,
    asker_for,
    progress_until,
    time_skipping,
    turns_for,
    worker_on,
)

pytestmark = pytest.mark.usefixtures("fixture_dataset")


def test_a_parked_run_shows_its_record_beside_its_position(
    stack: Stack, app_database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    async def status(env: Any, _handle: Any, parked: RunProgress) -> int:
        capsys.readouterr()
        return await operator_cli.status_report(env.client, app_database, parked.run_id)

    run_id, parked, code = propose_and_wait(stack, app_database, then=status)
    out = capsys.readouterr().out

    assert code == 0
    wait = parked.awaiting_approval
    assert f"record   waiting  human_approval  {wait}" in out, "the record's wait"
    assert "position workflow RUNNING" in out
    assert f"position awaiting approval {wait}, put 1x" in out, "Temporal's view of it"
    assert "position 1 cycles, 0 acts, 0 triggers queued" in out
    assert "STUCK" not in out
    assert run_id in out


def test_with_no_worker_alive_status_still_answers(
    stack: Stack, app_database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dead worker is exactly when someone runs this. The server describes the run; the
    progress query needs a worker, so it says none answered instead of hanging."""

    async def go() -> tuple[str, int]:
        async with time_skipping() as env:
            async with worker_on(
                env.client,
                "status",
                app_database,
                scorer=StubScorer(),
                rule=RULE,
                turns=turns_for(stack, DraftingEngine()),
                asker=asker_for(stack),
            ):
                run_id = await deliver(stack, env.client, PAYLOAD, source="t", task_queue="status")
                handle = env.client.get_workflow_handle_for(
                    ExperimentWorkflow.run, workflow_id(run_id)
                )
                await progress_until(handle, lambda p: p.awaiting_approval is not None)
            capsys.readouterr()
            return run_id, await operator_cli.status_report(env.client, app_database, run_id)

    run_id, code = asyncio.run(go())
    out = capsys.readouterr().out

    assert code == 0
    assert "record   waiting  human_approval" in out, "the record needs no worker"
    assert "position workflow RUNNING" in out
    assert "no worker answered" in out


class ScoringIsDown:
    """Fails every attempt the same way, and not as a refusal: so it is retried, forever."""

    model_version = StubScorer.model_version

    def score(self, **_: Any) -> ChurnScores:
        raise RuntimeError("the scoring service is down")


def test_an_activity_retried_past_the_threshold_is_flagged_with_its_last_failure(
    stack: Stack, app_database: Database, capsys: pytest.CaptureFixture[str]
) -> None:
    async def go() -> tuple[int, int]:
        async with (
            time_skipping() as env,
            worker_on(env.client, "stuck", app_database, scorer=ScoringIsDown(), rule=RULE),
        ):
            run_id = await deliver(stack, env.client, PAYLOAD, source="t", task_queue="stuck")
            attempts = 0
            for _ in range(60):
                # Backoff is a server timer (1s doubling to 1m): skipped, not slept.
                await env.sleep(timedelta(minutes=2))
                where = await position(env.client, run_id)
                assert where is not None
                attempts = max((a.attempt for a in where.pending), default=0)
                if attempts > STUCK_AFTER_ATTEMPTS:
                    break
            capsys.readouterr()
            return attempts, await operator_cli.status_report(env.client, app_database, run_id)

    attempts, code = asyncio.run(go())
    out = capsys.readouterr().out

    assert attempts > STUCK_AFTER_ATTEMPTS
    assert code == 1, "stuck is something a scheduler alerts on"
    assert "activity evaluate_cycle  attempt" in out and "STUCK" in out
    assert "last failure: the scoring service is down" in out


def test_status_of_a_run_the_record_does_not_know_asks_nobody_else(
    app_database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Answered from Postgres alone: the address is one nothing listens on."""
    code = operator_cli.main(
        ["--url", app_database_url, "status", "run-nobody-started", "--address", "localhost:1"]
    )

    assert code == 1
    assert "run-nobody-started is not a run" in capsys.readouterr().out
