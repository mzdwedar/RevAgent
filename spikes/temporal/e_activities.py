"""Activities for the E-probes. They reach the real gateway through the harness."""

from __future__ import annotations

from collections import Counter

from temporalio import activity
from temporalio.exceptions import ApplicationError

from agentstack.policy.approvers import authorize_approver

H = None  # the Harness, set by the probe before the worker starts
attempts: Counter[str] = Counter()


@activity.defn
def snapshot_of(charge: str) -> str:
    return H.snapshot(charge)


@activity.defn
def commit_refund(
    run_id: str, charge: str, key_mode: str, snapshot_from_workflow: str, lose_first_answer: bool
) -> str:
    info = activity.info()
    attempts[charge] += 1
    key = {
        "content": None,  # the tool builder's business key: refund:{tenant}:{charge}:{amount}
        "workflow+activity": f"{info.workflow_id}:{info.activity_id}",
        "run+activity": f"{info.workflow_run_id}:{info.activity_id}",
        "attempt": f"{info.workflow_run_id}:{info.activity_id}:{info.attempt}",
    }[key_mode]
    # Empty means "read the world now, at the act" - the other choice replays the
    # snapshot the workflow captured when the approver was asked.
    snapshot = snapshot_from_workflow or H.snapshot(charge)
    result = H.execute(run_id, H.refund(charge, key), snapshot)
    if lose_first_answer and info.attempt == 1:
        raise ApplicationError("the commit landed; the completion never reached the server")
    return result.receipt


@activity.defn
def grant_approval(run_id: str, charge: str, snapshot: str, claimed_user_id: str) -> str:
    """Layer 8 authority, as runtime/approvals.py does it: tenant from the run, never the reply."""
    run = H.stack.runs.get(run_id)
    approver = authorize_approver(
        H.stack.approver_directory, tenant=run.tenant, claimed_user_id=claimed_user_id
    )
    return H.grant(run_id, charge, snapshot, approver=approver.principal).id


@activity.defn
def rogue_refund(charge: str) -> str:
    """Reaches the surface client directly, around the gateway."""
    return H.stack.client.commit(f"acme/customers/c-42/charges/{charge}", {"amount_cents": 1999})
