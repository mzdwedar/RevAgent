"""E3: is the approval still immediately before the act when Temporal sits in between?"""

import asyncio
import contextlib
import uuid

from _eworker import running
from e_workflows import ApproveThenAct
from temporalio.service import RPCError


async def scenario(env, h, snapshot_mode: str, who: str, world_moves: bool):
    run, ch = h.new_run(), h.new_charge()
    wf = await env.client.start_workflow(
        ApproveThenAct.run,
        args=[run.run_id, ch, snapshot_mode],
        id=f"e3-{uuid.uuid4()}",
        task_queue="e3",
    )
    accepted = await wf.execute_update(ApproveThenAct.respond, who)  # validator: shape only
    await asyncio.sleep(0.5)
    if world_moves:
        h.world[ch] += 1  # the charge changed after the approver looked at it
    with contextlib.suppress(RPCError):  # a refused run has already finished
        await wf.signal(ApproveThenAct.proceed)
    return accepted, await wf.result(), h.commits_for(ch)


async def main():
    async with running("e3") as (env, h):
        h.stack.approver_directory.add(
            tenant="acme", slack_user_id="U-FIN", principal="finance-oncall", added_by="spike"
        )
        rows = {
            "snapshot carried from the workflow, world moved": ("workflow", "U-FIN", True),
            "snapshot read in the activity,     world moved": ("activity", "U-FIN", True),
            "snapshot read in the activity,     world still": ("activity", "U-FIN", False),
            "unauthorised approver, update accepted       ": ("activity", "U-RANDO", False),
        }
        got = {}
        for label, args in rows.items():
            got[label] = await scenario(env, h, *args)
            print(
                f"  {label}: update={got[label][0]!r} outcome={got[label][1]!r} "
                f"commits={got[label][2]}"
            )
        a, b, c, d = got.values()
        assert a[1].startswith("receipt") and a[2] == 1, a  # stale approval honoured
        assert b == ("accepted", "refused:ApprovalStale", 0), b
        assert c[1].startswith("receipt") and c[2] == 1, c
        assert d == ("accepted", "refused:ApproverNotAuthorized", 0), d
        print(
            "E3 PASS: staleness holds only if the activity reads the world at the act; "
            "the validator accepted an unauthorised approver and layer 8 refused it"
        )


asyncio.run(main())
