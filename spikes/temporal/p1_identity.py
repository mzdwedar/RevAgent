"""P1: one run per id, decided by the server (cf. runtime/cycles.py claim)."""

import asyncio
import uuid

from _env import local_env
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker
from workflows import CycleWorkflow


async def main():
    async with (
        await local_env() as env,
        Worker(env.client, task_queue="p1", workflows=[CycleWorkflow]),
    ):
        c, wid = env.client, f"cycle-{uuid.uuid4()}"
        kw = dict(id=wid, task_queue="p1", id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE)
        # Two deliveries racing while the first is running.
        res = await asyncio.gather(
            *[
                c.start_workflow(
                    CycleWorkflow.run, True, id_conflict_policy=WorkflowIDConflictPolicy.FAIL, **kw
                )
                for _ in range(5)
            ],
            return_exceptions=True,
        )
        started = [r for r in res if not isinstance(r, Exception)]
        refused = [r for r in res if isinstance(r, WorkflowAlreadyStartedError)]
        assert (len(started), len(refused)) == (1, 4), res
        # USE_EXISTING: a redelivery attaches to the running one instead of failing.
        h = await c.start_workflow(
            CycleWorkflow.run, True, id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING, **kw
        )
        assert h.result_run_id == started[0].result_run_id
        await started[0].result()
        # After it closed, REJECT_DUPLICATE refuses a late redelivery.
        try:
            await c.start_workflow(CycleWorkflow.run, False, **kw)
            raise AssertionError("late redelivery was allowed")
        except WorkflowAlreadyStartedError:
            pass
        print(
            "P1 PASS: 1 of 5 concurrent starts won; USE_EXISTING attaches; "
            "closed id rejects redelivery"
        )


asyncio.run(main())
