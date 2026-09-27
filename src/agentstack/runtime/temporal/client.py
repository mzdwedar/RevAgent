"""What the rest of the system calls to reach a run: connect, and start.

Layer 1 hands off through here and resolves nothing itself. The workflow id is the
run's, so the server, not this code, decides that one run id is one workflow (P1).
"""

from __future__ import annotations

import asyncio
import os

from temporalio.client import Client, WorkflowHandle
from temporalio.common import WorkflowIDConflictPolicy

from agentstack.runtime.temporal.contracts import TASK_QUEUE, RunEnd, RunStart, workflow_id
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
