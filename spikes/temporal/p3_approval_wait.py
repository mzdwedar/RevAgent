"""P3: approval as an Update with a validator; timer re-ask (cf. waits.py, deadlines.py)."""

import asyncio
import uuid

from _env import local_env
from temporalio.client import WorkflowUpdateFailedError
from temporalio.service import RPCError
from temporalio.worker import Worker
from workflows import ApprovalWorkflow, ledger_lines, record


async def main():
    async with (
        await local_env() as env,
        Worker(env.client, task_queue="p3", workflows=[ApprovalWorkflow], activities=[record]),
    ):
        tag = str(uuid.uuid4())
        h = await env.client.start_workflow(
            ApprovalWorkflow.run, args=[tag, 1.0, 5], id=tag, task_queue="p3"
        )
        await asyncio.sleep(2.5)  # let two re-asks happen
        pending = await h.query(ApprovalWorkflow.pending)
        assert pending in ("wait-3", "wait-2"), pending
        # A stale answer (to a question already re-asked) is refused by the validator.
        try:
            await h.execute_update(ApprovalWorkflow.respond, args=["wait-1", "yes"])
            raise AssertionError("stale answer accepted")
        except (WorkflowUpdateFailedError, RPCError) as e:
            print(f"  stale answer refused: {type(e).__name__}")
        pending = await h.query(ApprovalWorkflow.pending)
        assert await h.execute_update(ApprovalWorkflow.respond, args=[pending, "yes"]) == "accepted"
        try:
            await h.execute_update(ApprovalWorkflow.respond, args=[pending, "no"])
            raise AssertionError("second answer accepted")
        except (WorkflowUpdateFailedError, RPCError) as e:
            print(f"  second answer refused: {type(e).__name__}")
        assert await h.result() == "yes"
        whats = [r["what"] for r in ledger_lines(tag)]
        asks = [w for w in whats if w.startswith("ask:")]
        assert whats[0] == "prepare" and whats[-1] == "commit:yes" and len(asks) >= 2, whats
        # Rejected updates leave no trace in history.
        hist = await h.fetch_history()
        accepted = [
            e
            for e in hist.events
            if e.HasField("workflow_execution_update_accepted_event_attributes")
        ]
        assert len(accepted) == 1, len(accepted)
        print(
            f"P3 PASS: {len(asks)} asks, stale+duplicate answers refused by validator, "
            "1 update in history"
        )


asyncio.run(main())
