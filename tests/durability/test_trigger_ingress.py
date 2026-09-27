"""T37: a trigger payload reaches its experiment's run, and only its record decides which.

The ingress parses and hands off (layer 1). The composition root finds the experiment's
run in Postgres, or makes it the first time, and signal-with-starts the workflow. Three
properties are asserted here, each against a real worker:

* the first trigger for an experiment makes one session, one run and one mapping, however
  many first deliveries race;
* a trigger redelivered after the workflow **closed** evaluates once. Temporal starts a
  new execution under the same id, because reuse is allowed on purpose, and the
  Postgres cycle claim is what stops the second evaluation (criterion 30);
* a malformed payload writes nothing.
"""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from temporalio.client import Client

from agentstack.interfaces.triggers import MalformedTrigger, parse_trigger
from agentstack.interfaces.wiring import (
    EXPERIMENT_OPERATOR,
    Stack,
    build_stack,
    deliver,
    run_for_trigger,
)
from agentstack.runtime.temporal.contracts import RunProgress, RunStart, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database
from tests.fitness.test_trigger_to_candidate import RULE, WATERMARK, StubScorer
from tests.temporal_support import progress_until, running_worker

pytestmark = pytest.mark.usefixtures("fixture_dataset")


def payload(experiment_id: str = "exp-7", **overrides: str) -> dict[str, Any]:
    return {
        "kind": "data_arrival",
        "experiment_id": experiment_id,
        "data_as_of": WATERMARK,
        "tenant": "acme",
    } | overrides


@pytest.fixture
def stack(app_database: Database, checkpointer: Any) -> Stack:
    return build_stack(app_database, checkpointer)


def count(db: Database, table: str) -> int:
    row = db.fetch_one(f"SELECT count(*) FROM {table}")  # table names are literals here
    assert row is not None
    return int(row[0])


def cycles_of(client: Client, run_id: str) -> Any:
    return client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))


def test_the_first_trigger_makes_the_experiments_run_from_the_record(stack: Stack) -> None:
    start = run_for_trigger(stack, parse_trigger(payload(), source="test"))

    session = stack.sessions.get(start.session_id)
    assert session is not None
    assert session.user_id == EXPERIMENT_OPERATOR and session.tenant == "acme"
    run = stack.runs.get(start.run_id)
    assert run is not None and run.channel == "trigger"
    assert run_for_trigger(stack, parse_trigger(payload(), source="test")) == start
    assert run_for_trigger(stack, parse_trigger(payload("exp-8"), source="test")) != start


def test_racing_first_deliveries_make_one_session_and_one_run(
    stack: Stack, app_database: Database
) -> None:
    """Both read "nothing here"; the claim decides, and the loser writes nothing."""
    event = parse_trigger(payload(), source="test")
    barrier = threading.Barrier(8)

    def deliver_once(_: int) -> RunStart:
        barrier.wait()
        return run_for_trigger(stack, event)

    with ThreadPoolExecutor(max_workers=8) as pool:
        starts = set(pool.map(deliver_once, range(8)))

    assert len(starts) == 1
    assert count(app_database, "experiment_runs") == 1
    assert count(app_database, "sessions") == 1
    assert count(app_database, "runs") == 1


def test_a_trigger_redelivered_after_the_run_closed_evaluates_once(
    stack: Stack, app_database: Database, temporal_address: str, task_queue: str
) -> None:
    """Criterion 30. Temporal would start a fresh execution here, and does; the claim holds."""
    scorer = StubScorer()

    async def deliver_twice_around_a_close() -> tuple[RunProgress, str | None, str | None]:
        async with running_worker(
            temporal_address, task_queue, app_database, scorer=scorer, rule=RULE
        ) as client:
            run_id = await deliver(stack, client, payload(), source="test", task_queue=task_queue)
            handle = cycles_of(client, run_id)
            await progress_until(handle, lambda p: len(p.cycles) == 1)
            first = (await handle.describe()).run_id
            await handle.terminate("the run closed; retention would forget it in time")

            again = await deliver(stack, client, payload(), source="test", task_queue=task_queue)
            assert again == run_id, "the redelivery finds the same run in the record"
            handle = cycles_of(client, run_id)
            progress = await progress_until(handle, lambda p: len(p.cycles) == 1)
            return progress, first, (await handle.describe()).run_id

    progress, first_execution, second_execution = asyncio.run(deliver_twice_around_a_close())

    assert first_execution != second_execution, "Temporal did start a second execution"
    assert scorer.calls == 1, "and the Postgres claim stopped it evaluating again"
    assert count(app_database, "trigger_cycles") == 1
    (cycle,) = progress.cycles
    assert cycle.outcome == "propose", "the second execution reads back the first's cycle"


def test_every_delivery_reaches_the_one_run_while_it_is_open(
    stack: Stack, app_database: Database, temporal_address: str, task_queue: str
) -> None:
    scorer = StubScorer()

    async def three_deliveries() -> RunProgress:
        async with running_worker(
            temporal_address, task_queue, app_database, scorer=scorer, rule=RULE
        ) as client:
            run_ids = {
                await deliver(stack, client, payload(), source="test", task_queue=task_queue)
                for _ in range(3)
            }
            (run_id,) = run_ids
            return await progress_until(cycles_of(client, run_id), lambda p: len(p.cycles) == 3)

    progress = asyncio.run(three_deliveries())

    assert scorer.calls == 1
    assert {c.outcome for c in progress.cycles} == {"propose"}


def test_a_malformed_trigger_writes_nothing(stack: Stack, app_database: Database) -> None:
    async def deliver_garbage() -> None:
        # Never connected: parsing refuses before the client is touched.
        await deliver(stack, Client.__new__(Client), payload(kind="bogus"), source="test")

    with pytest.raises(MalformedTrigger):
        asyncio.run(deliver_garbage())

    assert count(app_database, "experiment_runs") == 0
    assert count(app_database, "sessions") == 0
