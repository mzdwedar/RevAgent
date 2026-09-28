"""Waiting on durable timers: days of it, in under a second (P8, T38).

A run parks a `trigger` wait whenever it has no trigger to work on. The row holds the
deadline, which comes from `experiments/cadence.toml`, and `operator stalled` reads that
row exactly as before. The workflow's timer adds only position: the run knows it's
overdue. Nothing expires. A run past its deadline goes on waiting, and a late trigger
still settles it.

These run on Temporal's time-skipping test server, so the deadline is the real one
(36h by default) and not a shortened stand-in.

A run lives for weeks, so it also continues as new every hundred cycles (T46). That is
Temporal's bookkeeping, and the record must not be able to tell.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from datetime import timedelta
from typing import Any

import pytest
from temporalio.client import Client, WorkflowHandle

from agentstack.interfaces import operator_cli
from agentstack.interfaces.triggers import parse_trigger
from agentstack.interfaces.wiring import Stack, build_stack, run_for_trigger
from agentstack.runtime.cadence import TriggerCadence
from agentstack.runtime.temporal.client import start_run
from agentstack.runtime.temporal.contracts import (
    RunEnd,
    RunProgress,
    RunStart,
    Trigger,
    TriggerArrived,
    TriggerWaitIntent,
)
from agentstack.runtime.temporal.workflows import CONTINUE_EVERY, ExperimentWorkflow
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


CYCLES = 250
# Handed over before any worker polls, so the first execution meets all of them at once.
QUEUED_AHEAD = 150


async def executions(
    client: Client, handle: WorkflowHandle[ExperimentWorkflow, RunEnd]
) -> list[RunStart]:
    """What each of Temporal's executions of this run was started with, first to
    current, following each history to the execution it continued as."""
    run_id = handle.result_run_id
    starts: list[RunStart] = []
    while run_id:
        history = await client.get_workflow_handle(handle.id, run_id=run_id).fetch_history()
        started = history.events[0].workflow_execution_started_event_attributes
        [start] = await client.data_converter.decode(started.input.payloads, [RunStart])
        starts.append(start)
        continued = history.events[-1].workflow_execution_continued_as_new_event_attributes
        run_id = continued.new_execution_run_id
    return starts


def test_continuing_as_new_is_invisible_to_the_record(
    app_database: Database, run_start: RunStart, capsys: pytest.CaptureFixture[str]
) -> None:
    """Criterion 44: 250 cycles, three executions, one run, one record.

    150 triggers are queued before a worker polls, so the first handoff, at 100, carries
    50 the run hasn't evaluated. The run then parks its first trigger wait, and the other
    100 arrive while it runs, so the second handoff carries the wait count: a reset
    would park `…-trigger-0` again and be handed back a wait long since satisfied.

    Each trigger names a batch the snapshot on disk isn't, so the cycle abstains before
    scoring: this is about the handoff, not about what a cycle concludes.

    The worker is never stopped mid-test: the time-skipping server never moves a stopped
    worker's sticky task back to the shared queue, and the run would sit there.
    """
    queue = f"timers-{uuid.uuid4()}"
    experiment = f"exp-{uuid.uuid4().hex[:6]}"
    batches = [
        Trigger(
            kind="data_arrival",
            experiment_id=experiment,
            data_as_of=f"fixture:batch-{n}",
            tenant=run_start.tenant,
            source="test",
        )
        for n in range(CYCLES)
    ]

    def evaluated(count: int) -> Callable[[RunProgress], bool]:
        return lambda p: p.cycles_before + len(p.cycles) == count and p.waiting_on is not None

    async def run_through_the_handoffs() -> tuple[RunProgress, RunProgress, list[RunStart]]:
        async with time_skipping() as env:
            handle = await start_run(env.client, run_start, task_queue=queue)
            for batch in batches[:QUEUED_AHEAD]:
                await handle.signal(ExperimentWorkflow.trigger, batch)
            async with worker_on(env.client, queue, app_database, trigger_deadline=DEADLINE):
                parked = await progress_until(handle, evaluated(QUEUED_AHEAD), timeout=60)
                for batch in batches[QUEUED_AHEAD:]:
                    await handle.signal(ExperimentWorkflow.trigger, batch)
                last = await progress_until(handle, evaluated(CYCLES), timeout=60)
                capsys.readouterr()
                status_code.append(
                    await operator_cli.status_report(env.client, app_database, run_start.run_id)
                )
                return parked, last, await executions(env.client, handle)

    status_code: list[int] = []
    parked, last, starts = asyncio.run(run_through_the_handoffs())
    status = capsys.readouterr().out

    # The operator sees one run through all three executions (criterion 44).
    assert status_code == [0]
    assert status.count(f"run      {run_start.run_id}") == 1
    assert f"position {CYCLES} cycles" in status, "counted across the handoffs, not since the last"

    # Temporal's side: three executions, each started with what the one before carried.
    assert len(starts) == 1 + CYCLES // CONTINUE_EVERY, "continued as new at 100 and 200"
    assert {start.run_id for start in starts} == {run_start.run_id}
    assert starts[0].carried is None
    handoff = starts[1].carried
    assert handoff is not None
    assert (handoff.cycles_before, handoff.waits_parked) == (100, 0)
    assert handoff.pending == tuple(batches[100:QUEUED_AHEAD]), "in the order they arrived"
    assert handoff.last_watermark == "fixture:batch-99"
    later = starts[2].carried
    assert later is not None and later.cycles_before == 200 and later.waits_parked >= 1

    # The run's side: one run, as progress reports it.
    assert (parked.run_id, last.run_id) == (run_start.run_id, run_start.run_id)
    assert (parked.cycles_before, len(parked.cycles)) == (100, 50)
    assert parked.waiting_on == f"wait-{run_start.run_id}-trigger-0"
    assert (last.cycles_before, len(last.cycles)) == (200, 50)
    assert {cycle.outcome for cycle in last.cycles} == {"abstain"}

    # The record, which is what the run is audited from. Temporal's history never is.
    assert app_database.fetch_all("SELECT run_id FROM runs") == [(run_start.run_id,)], (
        "one run, recorded once, however many executions ensured it"
    )
    settled = app_database.fetch_one(
        "SELECT count(*), count(DISTINCT data_as_of) FROM trigger_cycles "
        "WHERE experiment_id = %s AND outcome = 'abstain' AND settled_at IS NOT NULL",
        (experiment,),
    )
    assert settled == (CYCLES, CYCLES), "every trigger evaluated once, none lost in a handoff"
    rows = app_database.fetch_all(
        "SELECT wait_id, satisfied, state_snapshot FROM waits WHERE run_id = %s",
        (run_start.run_id,),
    )
    # How many waits the live second half parks depends on how fast its triggers land;
    # what may not vary is that they form one sequence with one pending wait at its end.
    waits = sorted(rows, key=lambda row: int(str(row[0]).rsplit("-", 1)[1]))
    assert [row[0] for row in waits] == [
        f"wait-{run_start.run_id}-trigger-{n}" for n in range(len(waits))
    ], "one sequence of waits across every execution, no number parked twice"
    assert waits[0][1:] == (True, "fixture:batch-149")
    assert all(satisfied for _, satisfied, _ in waits[:-1])
    assert waits[-1][1:] == (False, "fixture:batch-249"), "one wait pending, after the last batch"
    assert last.waiting_on == waits[-1][0], "and the run is on the wait the record says"
