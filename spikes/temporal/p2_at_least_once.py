"""P2: activity retries re-run a committed effect; the idempotency key is still ours."""

import asyncio
import uuid

from _env import local_env
from temporalio.worker import Worker
from workflows import ChargeWorkflow, charge, ledger_lines


async def main():
    async with (
        await local_env() as env,
        Worker(env.client, task_queue="p2", workflows=[ChargeWorkflow], activities=[charge]),
    ):
        for use_key, expected in ((False, 2), (True, 1)):
            tag = str(uuid.uuid4())
            await env.client.execute_workflow(
                ChargeWorkflow.run, args=[tag, use_key], id=tag, task_queue="p2"
            )
            n = len(ledger_lines(tag))
            assert n == expected, (use_key, n)
            print(f"  idempotency key={use_key}: external effect landed {n}x")
        print("P2 PASS: without our key a retried activity double-charges; with it, once")


asyncio.run(main())
