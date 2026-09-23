"""A human's answer, turned into an approval the gateway will accept (criterion 23).

The step this exists for is the one that only shows up across a process death. The
process that parked the wait held the prepared `ActionRequest` and the rendered prompt
in memory. An hour later a different process handles the click, and it has neither -
so the wait carries the action fingerprint and the summary, and this reads them back.

Order is deliberate, and each step refuses rather than continuing:

1. **Resolve the run.** Everything after this is scoped by what it says, including the
   tenant. Taking the tenant from the interaction instead is the confused deputy T16
   is built to avoid.
2. **Authorise the approver** against that tenant (layer 8, criterion 25).
3. **Find the wait**, and check it belongs to this run and is still pending.
4. **Grant the approval**, bound to the fingerprint the wait recorded.
5. **Satisfy the wait.**

Nothing here commits the rollout. The next turn does, through the gateway, with the
approval now findable - which keeps the commit on the one path that has idempotency,
containment and an audit record.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentstack.policy.approval import ApprovalRecord, ApprovalStore
from agentstack.policy.approvers import (
    ApprovalReply,
    ApproverDirectory,
    authorize_approver,
)
from agentstack.runtime.run import RunStore
from agentstack.runtime.waits import ResumeEvent, ResumeRejected, WaitStore, resume


class ReplyNotApplicable(RuntimeError):
    """The answer does not correspond to a question this system is still asking."""


@dataclass(frozen=True, slots=True)
class Resolution:
    """What the human's answer did."""

    run_id: str
    wait_id: str
    approved: bool
    approver: str
    approval_id: str | None


@dataclass(frozen=True, slots=True)
class ApprovalCoordinator:
    runs: RunStore
    waits: WaitStore
    approvals: ApprovalStore
    directory: ApproverDirectory

    def apply(self, reply: ApprovalReply) -> Resolution:
        run = self.runs.get(reply.run_id)
        if run is None:
            raise ReplyNotApplicable(
                f"{reply.run_id} is not a run; an answer to a question nobody asked"
            )

        # The tenant comes from the run. The interaction does not carry one, and if it
        # did it would not be consulted.
        approver = authorize_approver(
            self.directory, tenant=run.tenant, claimed_user_id=reply.slack_user_id
        )

        wait = self.waits.get(reply.wait_id)
        if wait is None or wait.run_id != reply.run_id:
            raise ReplyNotApplicable(f"{reply.wait_id} is not a pending wait on {reply.run_id}")
        if wait.satisfied:
            raise ReplyNotApplicable(
                f"{reply.wait_id} was already answered; the second reply to one "
                "question is not a second decision"
            )
        if not wait.action_fingerprint or not wait.approval_summary:
            raise ReplyNotApplicable(
                f"{reply.wait_id} does not record what it was asking about, so no "
                "approval can be bound to it"
            )

        record: ApprovalRecord | None = None
        if reply.approved:
            record = self.approvals.grant_for_fingerprint(
                run_id=run.run_id,
                # The fingerprint the wait recorded, not one recomputed here. Anything
                # recomputed could differ from what the approver was actually shown.
                action_fingerprint=wait.action_fingerprint,
                state_snapshot=wait.state_snapshot,
                approver=approver.principal,
                summary=wait.approval_summary,
            )

        try:
            resume(
                self.waits,
                ResumeEvent(
                    run_id=run.run_id,
                    wait_id=wait.wait_id,
                    state_snapshot=wait.state_snapshot,
                    payload={
                        "approved_by": approver.principal if reply.approved else None,
                        "refused_by": None if reply.approved else approver.principal,
                        "slack_user_id": reply.slack_user_id,
                    },
                ),
            )
        except ResumeRejected as exc:
            raise ReplyNotApplicable(str(exc)) from exc

        return Resolution(
            run_id=run.run_id,
            wait_id=wait.wait_id,
            approved=reply.approved,
            approver=approver.principal,
            approval_id=None if record is None else record.id,
        )
