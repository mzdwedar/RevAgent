"""One worker: the workflow, its declared activities, and the limits they run under."""

from __future__ import annotations

from concurrent.futures import Executor

from temporalio.client import Client
from temporalio.worker import Worker

from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.contracts import TASK_QUEUE
from agentstack.runtime.temporal.workflows import ExperimentWorkflow


def build_worker(
    client: Client,
    *,
    activities: RunActivities,
    executor: Executor,
    task_queue: str = TASK_QUEUE,
) -> Worker:
    """The activities are synchronous, like the stores they call, so they run on
    `executor`. Its size bounds how many run at once."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[ExperimentWorkflow],
        activities=[activities.ensure_run],
        activity_executor=executor,
    )
