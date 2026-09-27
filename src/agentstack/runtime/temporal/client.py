"""What the rest of the system calls to reach a run: connect, and start.

Layer 1 hands off through here and resolves nothing itself. The workflow id is the
run's, so the server, not this code, decides that one run id is one workflow (P1).
"""

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy

from agentstack.runtime.temporal.contracts import TASK_QUEUE, RunEnd, RunStart, Trigger, workflow_id
from agentstack.runtime.temporal.workflows import ExperimentWorkflow

DEFAULT_ADDRESS = "localhost:7233"
CONNECT_TIMEOUT_S = 5.0


class TemporalUnavailable(RuntimeError):
    """The orchestrator is not there. Raised with the command that brings it up."""


def temporal_address() -> str:
    return os.environ.get("TEMPORAL_ADDRESS", DEFAULT_ADDRESS)


async def connect(address: str, *, timeout: float = CONNECT_TIMEOUT_S) -> Client:
    """A client, or a failure that says what to run.

    Bounded, because an unreachable address otherwise waits on the client's own
    retries, and a worker that hangs at start looks exactly like one that is working.
    """
    try:
        return await asyncio.wait_for(Client.connect(address), timeout=timeout)
    except Exception as exc:
        raise TemporalUnavailable(
            f"no Temporal server at {address} ({type(exc).__name__}: {exc}). "
            "Bring the substrate up with `bash scripts/dev_up.sh`."
        ) from exc


async def start_run(
    client: Client, start: RunStart, *, task_queue: str = TASK_QUEUE
) -> WorkflowHandle[ExperimentWorkflow, RunEnd]:
    """Start the run, or attach to it if it is already running.

    Attaching rather than failing: a caller that races another to start the same run
    wants that run, not an error saying someone else got there first.
    """
    return await client.start_workflow(
        ExperimentWorkflow.run,
        start,
        id=workflow_id(start.run_id),
        task_queue=task_queue,
        id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
    )


async def deliver_trigger(
    client: Client, start: RunStart, trigger: Trigger, *, task_queue: str = TASK_QUEUE
) -> WorkflowHandle[ExperimentWorkflow, RunEnd]:
    """Hand a trigger to its run, starting the run if it isn't running (signal-with-start).

    Not running includes *closed*. A redelivery after the run's workflow ended starts a
    new execution under the same id, which is allowed on purpose: the second evaluation
    is stopped by the Postgres cycle claim, not by Temporal remembering the first,
    because Temporal forgets once retention passes (criterion 30).

    `start` comes from the run's record, never from the trigger: the trigger only says
    something happened.
    """
    return await client.start_workflow(
        ExperimentWorkflow.run,
        start,
        id=workflow_id(start.run_id),
        task_queue=task_queue,
        start_signal="trigger",
        start_signal_args=[trigger],
    )


async def notify_answer(client: Client, *, run_id: str, wait_id: str) -> None:
    """Wake the run: the answer to `wait_id` has been authorised and recorded.

    Called only after layer 8 wrote it (ADR-0008 rule 4). The signal carries the wait's
    id and nothing else, so it grants nothing: whether it was a yes, and who said so,
    is read from the `approvals` row at the act. Lost, it costs one re-ask interval,
    because the next ask finds the wait answered.
    """
    handle = client.get_workflow_handle_for(ExperimentWorkflow.run, workflow_id(run_id))
    await handle.signal(ExperimentWorkflow.answered, wait_id)
