"""E2: the gateway's UnresolvedEffect meets Temporal's retry policy."""

import asyncio
import uuid

from _eworker import running
from e_activities import attempts
from e_workflows import Unresolved


async def main():
    async with running("e2") as (env, h):
        c = env.client
        # Default: every Python exception is retryable, so the run spins on IN_FLIGHT.
        run, ch = h.new_run(), h.new_charge()
        h.grant(run.run_id, ch, h.snapshot(ch))
        h.stack.client.fail_after_effect = True
        out = await c.execute_workflow(
            Unresolved.run, args=[run.run_id, ch, False], id=f"e2-{uuid.uuid4()}", task_queue="e2"
        )
        h.stack.client.fail_after_effect = False
        assert out == "failed:UnresolvedEffect" and attempts[ch] == 5 and h.commits_for(ch) == 1
        print(
            f"  retryable:     {attempts[ch]} attempts burnt, {h.commits_for(ch)} commit, "
            f"ends {out!r} - the ledger held, but nothing reconciles"
        )

        # Non-retryable: one attempt, the run parks for reconciliation, then deduplicates.
        run, ch = h.new_run(), h.new_charge()
        h.grant(run.run_id, ch, h.snapshot(ch))
        h.stack.client.fail_after_effect = True
        wf = await c.start_workflow(
            Unresolved.run, args=[run.run_id, ch, True], id=f"e2-{uuid.uuid4()}", task_queue="e2"
        )
        while not await wf.query(Unresolved.is_parked):
            await asyncio.sleep(0.1)
        h.stack.client.fail_after_effect = False
        (key,) = [k for k in h.stack.ledger.unresolved_keys() if f":{ch}:" in k]
        h.stack.ledger.finalize(key, "receipt-reconciled")  # what a reconciler does
        await wf.signal(Unresolved.reconcile_done)
        out = await wf.result()
        assert out == "receipt-reconciled" and attempts[ch] == 2 and h.commits_for(ch) == 1, (
            out,
            attempts[ch],
            h.commits_for(ch),
        )
        print(
            f"  non-retryable: 1 attempt, parked, reconciled, re-run deduplicated -> {out!r}, "
            f"{h.commits_for(ch)} commit"
        )
        print("E2 PASS: UnresolvedEffect must be non-retryable and route to a reconcile wait")


asyncio.run(main())
