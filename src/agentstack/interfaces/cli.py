"""A channel you can run, so the layer boundaries are observable rather than claimed.

    uv run agentstack

Walks one rollout through the whole stack: assembled context, exposed tools, a policy
decision, a wait parked because a human is needed, an approval bound to that exact
action, a resume against the same run id, the commit - and then a retry that is
deduplicated instead of rolling out twice.
"""

from __future__ import annotations

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, handle
from agentstack.runtime.loop import TurnResult
from agentstack.runtime.run import new_run
from agentstack.runtime.waits import ResumeEvent, resume
from agentstack.storage.checkpoints import open_checkpointer
from agentstack.storage.database import Database
from agentstack.storage.pool import open_pool
from agentstack.tools.experiments import ROLLOUT_STAGE

SCOPES = frozenset({"experiments:rollout"})
MESSAGE = (
    "roll out the discount: roll_out_variant_to_percentage tenant=acme experiment_id=exp-7 "
    "experiment_version=exp:v1 percentage=10 targeting_model_version=tabpfn-3.5 "
    "risk_threshold=0.61 prior_rollout_event=0"
)


def _report(label: str, result: TurnResult) -> None:
    print(f"\n--- {label}")
    print(f"  status   : {result.status}")
    print(f"  context  : {len(result.bundle.items)} items, fp={result.bundle.fingerprint()}")
    print(f"  spans    : {' '.join(sorted(result.tracer.names()))}")
    missing = sorted(result.tracer.missing_required())
    print(f"  missing  : {missing if missing else 'none of the required spans'}")
    if result.approval_summary:
        print(f"  asks     : {result.approval_summary}")
    if result.receipts:
        print(f"  receipts : {result.receipts}")


def main() -> None:
    checkpoints, saver = open_checkpointer()
    try:
        with open_pool(min_size=1, max_size=4) as pool:
            walk_through(build_stack(Database(pool=pool), saver))
    finally:
        checkpoints.close()


def walk_through(stack: Stack) -> None:
    session = stack.resolver.start(user_id="agent-operator", tenant="acme")
    event = InboundEvent(
        channel="cli",
        tenant="acme",
        user_id="agent-operator",
        session_id=session.session_id,
        text=MESSAGE,
    )
    # What an earlier drafting turn wrote: the registry will not roll out a draft it
    # has never seen. Only when nobody has, so the demo can be run again.
    if stack.registry_client.state("acme/experiments/exp-7") is None:
        stack.registry_client.commit(
            "acme/experiments/exp-7",
            {
                "experiment_version": "exp:v1",
                "hypothesis": "a discount retains",
                "variant": "20-off",
            },
        )
    run = new_run(
        session_id=session.session_id,
        tenant="acme",
        user="agent-operator",
        stage=ROLLOUT_STAGE,
        channel="cli",
    )

    first = handle(stack, event, scopes=SCOPES, run=run)
    _report("turn 1 - rollout prepared, run parked on a human approval", first)

    wait = first.pending_wait
    request = first.pending_request
    summary = first.approval_summary
    assert wait is not None and request is not None and summary is not None

    stack.approvals.grant(
        run_id=run.run_id,
        request=request,
        state_snapshot=wait.state_snapshot,
        approver="experiment-owner",
        summary=summary,
    )
    resume(
        stack.waits,
        ResumeEvent(
            run_id=run.run_id,
            wait_id=wait.wait_id,
            state_snapshot=wait.state_snapshot,
            payload={"approved_by": "experiment-owner"},
        ),
    )
    print(f"\napproval granted, bound to action {request.fingerprint()} in run {run.run_id}")

    second = handle(stack, event, scopes=SCOPES, run=run)
    _report("turn 2 - resumed against the same run, rollout committed", second)

    third = handle(stack, event, scopes=SCOPES, run=run)
    _report("turn 3 - retried, and the step boundary means it does not happen twice", third)

    print("\n--- evidence")
    print(f"  surface commits : {len(stack.registry_client.rollouts)} (one rollout, not three)")
    audited = stack.audit.for_run(run.run_id)
    print(f"  audit records   : {len(audited)} in a sink separate from traces")
    steps = [f"{r.name}:{r.status}" for r in stack.steps.records_for(run.run_id)]
    print(f"  steps recorded  : {steps}")
    print(f"  maintenance     : {stack.maintenance.jobs} (enqueued, never on the hot path)")


if __name__ == "__main__":
    main()
