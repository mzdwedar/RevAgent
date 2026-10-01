"""T36: a trigger drives one evaluation cycle through a real worker.

`test_trigger_to_candidate` and `test_trigger_asymmetry` prove the cycle's rules where
the cycle is called directly. These prove the same rules still hold once Temporal sits
in front of it: the cycle runs as an activity that is at-least-once, from a workflow
that is replayed, and neither is allowed to change what one trigger comes to.

No effect is wired. A cycle scores and claims; it doesn't draft, ask or roll out.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

from agentstack.interfaces.triggers import parse_trigger
from agentstack.interfaces.wiring import build_stack
from agentstack.policy.triggers import OutcomeNotAuthorized
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.temporal.client import start_run
from agentstack.runtime.temporal.contracts import CycleResult, RunProgress, RunStart, Trigger
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database
from tests.fitness.test_trigger_to_candidate import RULE, WATERMARK, StubScorer
from tests.temporal_support import activities_for, progress_until, running_worker

pytestmark = pytest.mark.usefixtures("fixture_dataset")

TENANT = "acme"


@pytest.fixture
def run_start(app_database: Database, checkpointer: Any) -> RunStart:
    session = build_stack(app_database, checkpointer, tenant=TENANT).resolver.start(
        user_id="agent-operator", tenant=TENANT
    )
    return RunStart(
        run_id=f"run-{uuid.uuid4()}", session_id=session.session_id, tenant=TENANT, user="op"
    )


def trigger(kind: str = "data_arrival", experiment_id: str = "exp-7") -> Trigger:
    return Trigger(
        kind=kind, experiment_id=experiment_id, data_as_of=WATERMARK, tenant=TENANT, source="test"
    )


def drive(
    address: str,
    queue: str,
    db: Database,
    start: RunStart,
    triggers: list[Trigger],
    scorer: StubScorer,
) -> RunProgress:
    """Start the run, hand it the triggers, and wait until it has answered every one."""

    async def go() -> RunProgress:
        async with running_worker(address, queue, db, scorer=scorer, rule=RULE) as client:
            handle = await start_run(client, start, task_queue=queue)
            for t in triggers:
                await handle.signal(ExperimentWorkflow.trigger, t)
            return await progress_until(handle, lambda p: len(p.cycles) == len(triggers))

    return asyncio.run(go())


def test_a_trigger_becomes_one_settled_cycle(
    temporal_address: str, task_queue: str, app_database: Database, run_start: RunStart
) -> None:
    scorer = StubScorer()

    progress = drive(temporal_address, task_queue, app_database, run_start, [trigger()], scorer)

    (cycle,) = progress.cycles
    assert cycle.outcome == "propose"
    assert cycle.experiment_version is not None
    assert cycle.refusal is None
    assert scorer.calls == 1
    settled = CycleStore(db=app_database).unsettled()
    assert settled == (), "a cycle that reached an outcome is settled in Postgres"


def test_a_redelivered_trigger_scores_once(
    temporal_address: str, task_queue: str, app_database: Database, run_start: RunStart
) -> None:
    """Criterion 30's mechanism: the Postgres claim deduplicates, not workflow history."""
    scorer = StubScorer()

    progress = drive(temporal_address, task_queue, app_database, run_start, [trigger()] * 3, scorer)

    assert scorer.calls == 1
    assert len({c.experiment_version for c in progress.cycles}) == 1
    rows = app_database.fetch_one("SELECT count(*) FROM trigger_cycles")
    assert rows == (1,)


def test_a_metric_movement_still_cannot_propose(
    temporal_address: str, task_queue: str, app_database: Database, run_start: RunStart
) -> None:
    """The asymmetry holds through the worker, and the refusal reaches the workflow.

    If `OutcomeNotAuthorized` were retried, the second attempt would find the cycle
    claimed and return it unsettled, as a success. So a refusal in the result is also
    the proof of exactly one attempt.
    """
    progress = drive(
        temporal_address,
        task_queue,
        app_database,
        run_start,
        [trigger(kind="metric_movement")],
        StubScorer(),
    )

    (cycle,) = progress.cycles
    assert cycle.refusal == "OutcomeNotAuthorized"
    assert cycle.outcome is None
    (unsettled,) = CycleStore(db=app_database).unsettled()
    assert unsettled.kind.value == "metric_movement", "the refusal stays visible, unsettled"


def test_a_refused_cycle_does_not_end_the_run(
    temporal_address: str, task_queue: str, app_database: Database, run_start: RunStart
) -> None:
    progress = drive(
        temporal_address,
        task_queue,
        app_database,
        run_start,
        [trigger(kind="metric_movement"), trigger(experiment_id="exp-8")],
        StubScorer(),
    )

    refused, next_one = progress.cycles
    assert refused.refusal == "OutcomeNotAuthorized"
    assert next_one.outcome == "propose"


def test_the_activity_rerun_after_it_wrote_is_one_cycle(
    app_database: Database, run_id: str
) -> None:
    """At-least-once (P2): a completion lost after the claim landed reruns the activity.
    Called twice directly, as the retry would, it converges without scoring again."""
    scorer = StubScorer()
    activities = activities_for(app_database, scorer=scorer, rule=RULE)

    first = activities.evaluate_cycle(trigger(), run_id)
    second = activities.evaluate_cycle(trigger(), run_id)

    assert first == second
    assert isinstance(first, CycleResult) and first.outcome == "propose"
    assert scorer.calls == 1
    assert app_database.fetch_one("SELECT count(*) FROM trigger_cycles") == (1,)


def test_a_cycle_whose_attempt_died_mid_scoring_is_finished_by_the_retry(
    app_database: Database, run_id: str
) -> None:
    """Checkpoint J, finding (a). The first attempt claimed the cycle and died before it
    could settle: the worker was killed while scoring. Temporal reruns the activity.

    Returning the claimed cycle as it stands would hand the workflow "no outcome" as a
    success, with nothing scored, and the run would move on past a watermark it never
    evaluated. Under Temporal the workflow is this cycle's only evaluator, and it runs
    one cycle at a time, so a claim it finds unsettled is its own dead attempt.
    """
    store = CycleStore(db=app_database)
    event = parse_trigger(
        {
            "kind": "data_arrival",
            "experiment_id": "exp-7",
            "data_as_of": WATERMARK,
            "tenant": TENANT,
        },
        source="test",
    )
    store.claim(event)  # the attempt that died: claimed, never settled
    scorer = StubScorer()

    result = activities_for(app_database, scorer=scorer, rule=RULE).evaluate_cycle(
        trigger(), run_id
    )

    assert result.outcome == "propose"
    assert result.experiment_version is not None
    assert scorer.calls == 1
    assert store.unsettled() == ()


def test_a_refused_cycle_is_refused_again_not_resumed_into_an_outcome(
    app_database: Database, run_id: str
) -> None:
    """The other way a cycle stays unsettled: layer 8 refused its outcome. Evaluating it
    again reaches the same refusal. It never becomes an outcome by being retried."""
    activities = activities_for(app_database, scorer=StubScorer(), rule=RULE)

    for _ in range(2):
        with pytest.raises(OutcomeNotAuthorized):
            activities.evaluate_cycle(trigger(kind="metric_movement"), run_id)

    (unsettled,) = CycleStore(db=app_database).unsettled()
    assert unsettled.kind.value == "metric_movement"
