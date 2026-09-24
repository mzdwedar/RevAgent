"""One Temporal dev server, one worker, one Harness - for the E-probes."""

from __future__ import annotations

import contextlib
from concurrent.futures import ThreadPoolExecutor

import e_activities
from _env import local_env
from _gateway import Harness
from e_activities import commit_refund, grant_approval, rogue_refund, snapshot_of
from e_workflows import ApproveThenAct, KeyDerivation, Rogue, Unresolved
from temporalio.worker import Worker

ACTIVITIES = [commit_refund, grant_approval, rogue_refund, snapshot_of]
WORKFLOWS = [ApproveThenAct, KeyDerivation, Rogue, Unresolved]


@contextlib.asynccontextmanager
async def running(task_queue: str, interceptors=()):
    h = Harness()
    e_activities.H = h
    try:
        async with await local_env() as env:
            with ThreadPoolExecutor(max_workers=8) as pool:
                async with Worker(
                    env.client,
                    task_queue=task_queue,
                    workflows=WORKFLOWS,
                    activities=ACTIVITIES,
                    activity_executor=pool,
                    interceptors=list(interceptors),
                ):
                    yield env, h
    finally:
        h.close()
