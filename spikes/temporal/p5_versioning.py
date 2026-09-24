"""P5: the Replayer catches a deploy that would break live runs (cf. T19/T20 checkpoint_guard)."""

import asyncio
import uuid

from _env import local_env
from p5_defs import V1, V2Patched, V2Unsafe
from temporalio import workflow
from temporalio.worker import Replayer, Worker
from workflows import record


async def main():
    async with (
        await local_env() as env,
        Worker(env.client, task_queue="p5", workflows=[V1], activities=[record]),
    ):
        tag = str(uuid.uuid4())
        h = await env.client.start_workflow(V1.run, tag, id=tag, task_queue="p5")
        await h.result()
        history = await h.fetch_history()
    try:
        await Replayer(workflows=[V2Unsafe]).replay_workflow(history)
        raise AssertionError("unsafe change replayed cleanly")
    except workflow.NondeterminismError as e:
        print(f"  V2Unsafe refused: {str(e)[:110]}...")
    await Replayer(workflows=[V2Patched]).replay_workflow(history)
    print(
        "P5 PASS: Replayer rejects the unguarded change, accepts the patched() one - offline, in CI"
    )


asyncio.run(main())
