"""One worker: the workflow, its declared activities, and the limits they run under."""

from __future__ import annotations

from concurrent.futures import Executor

from temporalio.client import Client
from temporalio.worker import Worker

from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.contracts import TASK_QUEUE
from agentstack.runtime.temporal.interceptors import DeclaredActivitiesOnly
from agentstack.runtime.temporal.workflows import ExperimentWorkflow


def build_worker(
    client: Client,
    *,
    activities: RunActivities,
    executor: Executor,
    task_queue: str = TASK_QUEUE,
) -> Worker:
    """The activities are synchronous, like the stores they call, so they run on
    `executor`. Its size bounds how many run at once. Registering an activity here does
    not declare it: `DeclaredActivitiesOnly` refuses anything `interceptors.DECLARED`
    doesn't name."""
    return Worker(
        client,
        task_queue=task_queue,
        workflows=[ExperimentWorkflow],
        activities=[
            activities.ensure_run,
            activities.evaluate_cycle,
            activities.park_trigger_wait,
            activities.satisfy_trigger_wait,
            activities.run_turn,
            activities.ask_approval,
        ],
        activity_executor=executor,
        interceptors=[DeclaredActivitiesOnly()],
    )
