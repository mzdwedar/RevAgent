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
from pathlib import Path
from typing import Any

import pytest

from agentstack.context.datasets import REGISTRY, CohortSnapshot, DatasetSpec
from agentstack.interfaces.wiring import build_stack
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.run import RunStore
from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.client import start_run
from agentstack.runtime.temporal.contracts import CycleResult, RunProgress, RunStart, Trigger
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database
from tests.fitness.test_trigger_to_candidate import RULE, WATERMARK, StubScorer, snapshot
from tests.temporal_support import progress_until, running_worker

TENANT = "acme"


@pytest.fixture(autouse=True)
def _fixture_dataset(monkeypatch: pytest.MonkeyPatch) -> Any:
    """The same fixture dataset `test_trigger_to_candidate` targets, loaded in-process:
    the worker runs its activities in this process, so the monkeypatch reaches them."""
    REGISTRY["fixture"] = DatasetSpec(
        key="fixture",
        kaggle="nobody/nothing",
        files=("fixture.csv",),
        target="Churn",
        churned="1",
        drops={},
        revenue_columns=("monthly charge",),
        revenue_periods_per_year=12,
        revenue_note="fixture revenue, annualised x12",
    )

    def only_the_fixture(key: str, root: Path | None = None) -> CohortSnapshot:
        assert key == "fixture", f"asked for {key!r}"
        assert root is None
        return snapshot()

    monkeypatch.setattr("agentstack.context.datasets.load", only_the_fixture)
    yield
    REGISTRY.pop("fixture", None)


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


def test_the_activity_rerun_after_it_wrote_is_one_cycle(app_database: Database) -> None:
    """At-least-once (P2): a completion lost after the claim landed reruns the activity.
    Called twice directly, as the retry would, it converges without scoring again."""
    scorer = StubScorer()
    activities = RunActivities(
        runs=RunStore(db=app_database),
        cycles=CycleStore(db=app_database),
        scorer=scorer,
        rule=RULE,
    )

    first = activities.evaluate_cycle(trigger())
    second = activities.evaluate_cycle(trigger())

    assert first == second
    assert isinstance(first, CycleResult) and first.outcome == "propose"
    assert scorer.calls == 1
    assert app_database.fetch_one("SELECT count(*) FROM trigger_cycles") == (1,)
