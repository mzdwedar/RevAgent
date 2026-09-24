"""Worker process for P4; killed with SIGKILL mid-wait."""

import asyncio
import sys

from temporalio.client import Client
from temporalio.worker import Worker
from workflows import ApprovalWorkflow, record


async def main(target: str):
    client = await Client.connect(target)
    async with Worker(client, task_queue="p4", workflows=[ApprovalWorkflow], activities=[record]):
        print("worker up", flush=True)
        await asyncio.Event().wait()


asyncio.run(main(sys.argv[1]))
