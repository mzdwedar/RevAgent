"""P4: SIGKILL the worker while the run is parked; a new process resumes it (cf. T17)."""

import asyncio
import signal
import subprocess
import sys
import uuid

from _env import local_env
from workflows import ApprovalWorkflow, ledger_lines


def spawn(target):
    p = subprocess.Popen(
        [sys.executable, "p4_worker.py", target], stdout=subprocess.PIPE, text=True
    )
    assert p.stdout.readline().strip() == "worker up"
    return p


async def main():
    async with await local_env() as env:
        target = env.client.service_client.config.target_host
        w1, tag = spawn(target), str(uuid.uuid4())
        h = await env.client.start_workflow(
            ApprovalWorkflow.run, args=[tag, 600.0, 3], id=tag, task_queue="p4"
        )
        while "ask:wait-1" not in [r["what"] for r in ledger_lines(tag)]:
            await asyncio.sleep(0.1)
        w1.send_signal(signal.SIGKILL)
        w1.wait()
        print("  worker 1 SIGKILLed while parked on wait-1")
        w2 = spawn(target)
        try:
            # Update is accepted only once a worker has replayed the run's state.
            assert (
                await h.execute_update(ApprovalWorkflow.respond, args=["wait-1", "yes"])
                == "accepted"
            )
            assert await h.result() == "yes"
        finally:
            w2.kill()
        whats = [r["what"] for r in ledger_lines(tag)]
        assert whats == ["prepare", "ask:wait-1", "commit:yes"], whats
        print(f"P4 PASS: a fresh process resumed the parked run; effects {whats} each ran once")


asyncio.run(main())
