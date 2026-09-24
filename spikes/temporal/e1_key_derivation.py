"""E1: where may an idempotency key come from, once Temporal retries and resets runs?"""

import asyncio
import subprocess
import uuid

from _env import CLI
from _eworker import running
from e_workflows import KeyDerivation

MODES = ["content", "workflow+activity", "run+activity", "attempt"]


async def main():
    async with running("e1") as (env, h):
        c, target = env.client, env.client.service_client.config.target_host
        table = {}
        for mode in MODES:
            run = h.new_run()
            # Retry: the commit lands, the completion is lost, Temporal retries.
            ch = h.new_charge()
            h.grant(run.run_id, ch, h.snapshot(ch))
            await c.execute_workflow(
                KeyDerivation.run,
                args=[run.run_id, ch, mode, True],
                id=f"e1-{uuid.uuid4()}",
                task_queue="e1",
            )
            retry = h.commits_for(ch)
            # Reset: an operator rewinds a finished run to its first task and it runs again.
            ch = h.new_charge()
            h.grant(run.run_id, ch, h.snapshot(ch))
            wid = f"e1-{uuid.uuid4()}"
            await c.execute_workflow(
                KeyDerivation.run, args=[run.run_id, ch, mode, False], id=wid, task_queue="e1"
            )
            subprocess.run(
                [
                    CLI,
                    "workflow",
                    "reset",
                    "-w",
                    wid,
                    "-t",
                    "FirstWorkflowTask",
                    "--reason",
                    "e1 probe",
                    "-y",
                    "--address",
                    target,
                ],
                check=True,
                capture_output=True,
            )
            await c.get_workflow_handle(wid).result()
            reset = h.commits_for(ch)
            # A second workflow for the same effect: a redelivered trigger started anew.
            ch = h.new_charge()
            h.grant(run.run_id, ch, h.snapshot(ch))
            for _ in range(2):
                await c.execute_workflow(
                    KeyDerivation.run,
                    args=[run.run_id, ch, mode, False],
                    id=f"e1-{uuid.uuid4()}",
                    task_queue="e1",
                )
            table[mode] = (retry, reset, h.commits_for(ch))
            print(
                f"  key={mode:<18} retry: {retry}x  reset: {reset}x  second workflow: "
                f"{table[mode][2]}x"
            )
        assert table["content"] == (1, 1, 1), table
        assert table["workflow+activity"] == (1, 1, 2), table
        assert table["run+activity"] == (1, 2, 2), table
        assert table["attempt"] == (2, 2, 2), table
        print("E1 PASS: only the content-derived key lands once under retry, reset and redelivery")


asyncio.run(main())
