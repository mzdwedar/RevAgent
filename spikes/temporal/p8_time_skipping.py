"""P8: a 24h re-ask deadline tested in seconds (cf. deadlines tests)."""

import asyncio
import time
import uuid

from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker
from workflows import ApprovalWorkflow, ledger_lines, record


async def main():
    t0 = time.monotonic()
    async with (
        await WorkflowEnvironment.start_time_skipping() as env,
        Worker(env.client, task_queue="p8", workflows=[ApprovalWorkflow], activities=[record]),
    ):
        tag = str(uuid.uuid4())
        out = await env.client.execute_workflow(
            ApprovalWorkflow.run, args=[tag, 24 * 3600.0, 3], id=tag, task_queue="p8"
        )
    asks = [r for r in ledger_lines(tag) if r["what"].startswith("ask:")]
    assert out == "unanswered" and len(asks) == 3, (out, asks)
    print(f"P8 PASS: 3 asks across 72 simulated hours in {time.monotonic() - t0:.1f}s wall-clock")


asyncio.run(main())
