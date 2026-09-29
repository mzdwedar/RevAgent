"""One worker: the workflow, its declared activities, and the limits they run under.

One data batch lands and every experiment watching it fires at once. Each of those runs
wants an evaluation, and scoring is the expensive part of the system: seconds of CPU and
a model's worth of memory each. Nothing about a trigger arriving means its evaluation has
to start that instant, so the worker holds a fixed number of activity slots and Temporal
queues the rest on the task queue (P7). That bound replaces `fanout.py` (T45).

Duplicates are not this module's job. A trigger delivered three times reaches its run's
one workflow three times, and the cycle claim makes that one scoring.
"""

from __future__ import annotations

from concurrent.futures import Executor, ThreadPoolExecutor

from temporalio.client import Client
from temporalio.worker import Worker

from agentstack.runtime.temporal.activities import RunActivities
from agentstack.runtime.temporal.contracts import TASK_QUEUE
from agentstack.runtime.temporal.interceptors import DeclaredActivitiesOnly
from agentstack.runtime.temporal.workflows import ExperimentWorkflow
from agentstack.storage.pool import DEFAULT_MAX_SIZE

# At or below the app pool (an activity holds one pooled connection at a time, so no
# activity waits for one) and a small multiple of a laptop's cores (so scoring does not
# oversubscribe the CPU). A number to revisit with real TabPFN scoring under load; the
# bound itself is the design. It counts every activity, not only evaluations: a turn or
# a commit in flight also holds a connection.
MAX_CONCURRENT_ACTIVITIES = min(8, DEFAULT_MAX_SIZE)


def activity_threads(limit: int = MAX_CONCURRENT_ACTIVITIES) -> ThreadPoolExecutor:
    """Exactly enough threads for `limit` synchronous activities at once."""
    return ThreadPoolExecutor(max_workers=limit, thread_name_prefix="activity")


def build_worker(
    client: Client,
    *,
    activities: RunActivities,
    executor: Executor,
    max_concurrent_activities: int = MAX_CONCURRENT_ACTIVITIES,
    task_queue: str = TASK_QUEUE,
) -> Worker:
    """The activities are synchronous, like the stores they call, so they run on
    `executor`. How many run at once is `max_concurrent_activities`, the worker's slot
    count: a task beyond it stays on the queue, unclaimed, where its timeout hasn't
    started. The executor must have at least that many threads. With fewer, the threads
    would be the real bound, and the tasks they can't start would already be claimed
    and burning their `start_to_close_timeout` in the executor's backlog.

    Registering an activity here does not declare it: `DeclaredActivitiesOnly` refuses
    anything `interceptors.DECLARED` doesn't name.
    """
    if max_concurrent_activities < 1:
        raise ValueError(f"max_concurrent_activities={max_concurrent_activities} would run nothing")
    # The attribute Temporal itself reads to warn about this; we refuse instead.
    threads = getattr(executor, "_max_workers", None)
    if isinstance(threads, int) and threads < max_concurrent_activities:
        raise ValueError(
            f"an executor of {threads} threads would bound activities below the declared "
            f"max_concurrent_activities={max_concurrent_activities}"
        )
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
            activities.commit,
        ],
        activity_executor=executor,
        max_concurrent_activities=max_concurrent_activities,
        interceptors=[DeclaredActivitiesOnly()],
    )
