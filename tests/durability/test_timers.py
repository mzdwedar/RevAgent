"""Waiting on durable timers: days of it, in under a second (P8, T38).

A run parks a `trigger` wait whenever it has no trigger to work on. The row holds the
deadline, which comes from `experiments/cadence.toml`, and `operator stalled` reads that
row exactly as before. The workflow's timer adds only position: the run knows it's
overdue. Nothing expires. A run past its deadline goes on waiting, and a late trigger
still settles it.

These run on Temporal's time-skipping test server, so the deadline is the real one
(36h by default) and not a shortened stand-in.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import timedelta
from typing import Any

import pytest

from agentstack.interfaces import operator_cli
from agentstack.interfaces.triggers import parse_trigger
from agentstack.interfaces.wiring import Stack, build_stack, run_for_trigger
from agentstack.runtime.cadence import TriggerCadence
from agentstack.runtime.temporal.client import start_run
from agentstack.runtime.temporal.contracts import (
    RunProgress,
    RunStart,
    Trigger,
    TriggerArrived,
    TriggerWaitIntent,
)
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.runtime.waits import WaitStore
from agentstack.storage.database import Database
from tests.fitness.test_trigger_to_candidate import RULE, WATERMARK, StubScorer
from tests.temporal_support import activities_for, progress_until, time_skipping, worker_on

pytestmark = pytest.mark.usefixtures("fixture_dataset")

DEADLINE = TriggerCadence.load().trigger_deadline


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


@pytest.fixture
def run_start(stack: Stack) -> RunStart:
    payload = {
        "kind": "data_arrival",
        "experiment_id": f"exp-{uuid.uuid4().hex[:6]}",
        "data_as_of": WATERMARK,
        "tenant": "acme",
    }
    return run_for_trigger(stack, parse_trigger(payload, source="test"))


def trigger(start: RunStart) -> Trigger:
    return Trigger(
        kind="data_arrival",
        experiment_id="exp-7",
        data_as_of=WATERMARK,
        tenant=start.tenant,
        source="test",
    )


def test_the_deadline_is_the_cadence_files() -> None:
    """The number lives in experiments/, not here: this only checks it's read."""
    assert timedelta(hours=36) == DEADLINE
    assert TriggerCadence.load("dev").trigger_deadline == timedelta(days=7)


def test_a_missing_trigger_is_stalled_and_a_late_one_still_settles_it(
    app_database: Database,
    app_database_url: str,
    run_start: RunStart,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Criterion 43: stalled still surfaces, read from `waits` as today."""
    waits = WaitStore(db=app_database)
    queue = f"timers-{uuid.uuid4()}"

    def operator_stalled_after_deadline(wait_id: str | None) -> int:
        # The row's deadline was written on the real clock, so "past it" is said on
        # that clock too: `now` is what `operator stalled` takes for this.
        wait = waits.get(wait_id or "")
        assert wait is not None and wait.deadline is not None
        return operator_cli.main(
            ["--url", app_database_url, "stalled"], now=wait.deadline + timedelta(minutes=1)
        )

    async def wait_past_the_deadline() -> tuple[RunProgress, RunProgress, RunProgress]:
        async with (
            time_skipping() as env,
            worker_on(
                env.client,
                queue,
                app_database,
                scorer=StubScorer(),
                rule=RULE,
                trigger_deadline=DEADLINE,
            ),
        ):
            handle = await start_run(env.client, run_start, task_queue=queue)
            parked = await progress_until(handle, lambda p: p.waiting_on is not None)

            await env.sleep(DEADLINE + timedelta(hours=1))
            overdue = await progress_until(handle, lambda p: p.overdue)
            capsys.readouterr()
            assert operator_stalled_after_deadline(overdue.waiting_on) == 1
            assert f"stalled  {overdue.waiting_on}" in capsys.readouterr().out

            await handle.signal(ExperimentWorkflow.trigger, trigger(run_start))
            settled = await progress_until(
                handle, lambda p: len(p.cycles) == 1 and p.waiting_on != overdue.waiting_on
            )
            # The run has already parked its next wait, whose deadline falls a few real
            # seconds after the first's; at this `now` that one is due too. What matters
            # is that the settled wait is no longer reported.
            operator_stalled_after_deadline(overdue.waiting_on)
            assert f"stalled  {overdue.waiting_on}" not in capsys.readouterr().out
            return parked, overdue, settled

    parked, overdue, settled = asyncio.run(wait_past_the_deadline())

    assert parked.waiting_on is not None and not parked.overdue
    assert overdue.waiting_on == parked.waiting_on, "overdue, and still waiting on the same wait"
    wait = waits.get(parked.waiting_on)
    assert wait is not None and wait.satisfied, "the late trigger settled the wait"
    assert settled.cycles[0].outcome == "propose"
    assert settled.waiting_on == f"wait-{run_start.run_id}-trigger-1", "and waits for the next"
    assert settled.overdue is False


def test_operator_stalled_reports_a_trigger_wait_past_its_deadline(
    app_database: Database,
    app_database_url: str,
    run_start: RunStart,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The same row `operator stalled` has always read, now parked by the workflow."""
    waits = WaitStore(db=app_database)
    queue = f"timers-{uuid.uuid4()}"

    async def park() -> str:
        async with (
            time_skipping() as env,
            worker_on(env.client, queue, app_database, trigger_deadline=DEADLINE),
        ):
            handle = await start_run(env.client, run_start, task_queue=queue)
            progress = await progress_until(handle, lambda p: p.waiting_on is not None)
            assert progress.waiting_on is not None
            return progress.waiting_on

    wait_id = asyncio.run(park())
    wait = waits.get(wait_id)
    assert wait is not None and wait.deadline is not None

    before = operator_cli.main(
        ["--url", app_database_url, "stalled"], now=wait.deadline - timedelta(minutes=1)
    )
    assert before == 0, "a wait inside its deadline is waiting, not stalled"

    after = operator_cli.main(
        ["--url", app_database_url, "stalled"], now=wait.deadline + timedelta(minutes=1)
    )
    assert after == 1
    assert f"stalled  {wait_id}" in capsys.readouterr().out


def test_nothing_expires_however_long_the_trigger_takes(
    app_database: Database, run_start: RunStart
) -> None:
    """Seventy-two simulated hours on a 36-hour deadline: still waiting, on the same wait."""
    queue = f"timers-{uuid.uuid4()}"

    async def wait_three_days() -> tuple[RunProgress, RunProgress]:
        async with (
            time_skipping() as env,
            worker_on(env.client, queue, app_database, trigger_deadline=DEADLINE),
        ):
            handle = await start_run(env.client, run_start, task_queue=queue)
            first = await progress_until(handle, lambda p: p.waiting_on is not None)
            await env.sleep(timedelta(hours=72))
            return first, await progress_until(handle, lambda p: p.overdue)

    first, later = asyncio.run(wait_three_days())

    assert later.waiting_on == first.waiting_on
    assert later.cycles == ()
    assert len(WaitStore(db=app_database).pending_for(run_start.run_id)) == 1


def test_the_park_activity_rerun_is_one_wait_with_its_first_deadline(
    app_database: Database, run_start: RunStart
) -> None:
    """At-least-once (P2): a lost completion reruns the park. Same wait, same deadline."""
    activities = activities_for(app_database, trigger_deadline=DEADLINE)
    intent = TriggerWaitIntent(run_id=run_start.run_id, sequence=0, after_data_as_of="none")

    first = activities.park_trigger_wait(intent)
    second = activities.park_trigger_wait(intent)

    assert first.wait_id == second.wait_id
    assert second.due_in_s <= first.due_in_s, "the rerun did not push the deadline out"
    assert len(WaitStore(db=app_database).pending_for(run_start.run_id)) == 1


def test_the_satisfy_activity_rerun_converges(app_database: Database, run_start: RunStart) -> None:
    activities = activities_for(app_database, trigger_deadline=DEADLINE)
    parked = activities.park_trigger_wait(
        TriggerWaitIntent(run_id=run_start.run_id, sequence=0, after_data_as_of="none")
    )
    arrived = TriggerArrived(wait_id=parked.wait_id, trigger=trigger(run_start))

    assert activities.satisfy_trigger_wait(arrived) == parked.wait_id
    assert activities.satisfy_trigger_wait(arrived) == parked.wait_id
    wait = WaitStore(db=app_database).get(parked.wait_id)
    assert wait is not None and wait.satisfied
    assert wait.payload["data_as_of"] == WATERMARK
