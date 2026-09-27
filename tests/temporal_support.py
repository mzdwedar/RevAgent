"""A real worker in the test's own process, and a bounded way to watch a run.

The run is open-ended (one per experiment, living for weeks), so a test can't await
its result. It reads the `progress` query until what it expects is there, and fails
when that doesn't happen in time. A failing activity is retried until its policy
says stop, so every wait here is bounded: a failure fails instead of hanging.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from typing import Any

from temporalio.client import Client, WorkflowHandle

from agentstack.context.targeting import TargetingRule
from agentstack.prediction.churn import ChurnScores
from agentstack.runtime.cycles import CycleStore
from agentstack.runtime.run import RunStore
from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.contracts import RunProgress
from agentstack.runtime.temporal.worker import build_worker
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.database import Database
from tests.conftest import connect_temporal

WAIT_S = 30.0


class NoScoring:
    """For runs that are never handed a trigger. Scoring one would be a test bug."""

    model_version = "none"

    def score(self, **_: Any) -> ChurnScores:
        raise AssertionError("this test's run was not meant to score anything")


@asynccontextmanager
async def running_worker(
    address: str,
    task_queue: str,
    db: Database,
    *,
    scorer: Any = None,
    rule: TargetingRule | None = None,
) -> AsyncIterator[Client]:
    client = await connect_temporal(address)
    activities = RunActivities(
        runs=RunStore(db=db),
        cycles=CycleStore(db=db),
        scorer=scorer or NoScoring(),
        rule=rule,
    )
    with ThreadPoolExecutor(max_workers=4) as executor:
        worker = build_worker(
            client, activities=activities, executor=executor, task_queue=task_queue
        )
        async with worker:
            yield client


async def progress_until(
    handle: WorkflowHandle[ExperimentWorkflow, Any],
    done: Callable[[RunProgress], bool],
    *,
    timeout: float = WAIT_S,
) -> RunProgress:
    """Poll the run's position until `done` says so, or fail with where it got to."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        progress = await handle.query(ExperimentWorkflow.progress)
        if done(progress):
            return progress
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"run did not get there in {timeout}s; it is at {progress}")
        await asyncio.sleep(0.1)
