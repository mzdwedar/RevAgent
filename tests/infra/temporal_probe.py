"""A workflow with nothing in it, for proving the substrate answers.

Its own module because the workflow sandbox re-imports whatever defines a workflow,
and a test module drags pytest and every fixture in with it.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from temporalio import workflow
from temporalio.client import Client
from temporalio.worker import Worker


@workflow.defn
class Echo:
    @workflow.run
    async def run(self, text: str) -> str:
        return text


@asynccontextmanager
async def echo_worker(client: Client, task_queue: str) -> AsyncIterator[Worker]:
    async with Worker(client, task_queue=task_queue, workflows=[Echo]) as worker:
        yield worker
