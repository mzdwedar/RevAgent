"""P7: bounded fan-out is worker config (cf. runtime/fanout.py)."""

import asyncio
import uuid

from _env import local_env
from temporalio.worker import Worker
from workflows import FanoutWorkflow, peak, score


async def main():
    async with (
        await local_env() as env,
        Worker(
            env.client,
            task_queue="p7",
            workflows=[FanoutWorkflow],
            activities=[score],
            max_concurrent_activities=2,
        ),
    ):
        n = await env.client.execute_workflow(
            FanoutWorkflow.run, 10, id=str(uuid.uuid4()), task_queue="p7"
        )
        assert n == 10 and peak() == 2, (n, peak())
        print(f"P7 PASS: 10 scorings, peak concurrency {peak()} with max_concurrent_activities=2")


asyncio.run(main())
