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

**Every answer is an accountability record** (A1, H3): a yes, a no, an outsider's click
and an answer to a question no longer being asked each write one to the audit sink,
naming who answered, the wait, the fingerprint and the snapshot. The wait's own payload
is operational state and goes when the session does; the audit record does not. Without
it, a considered "no", a forged wake-up and an outsider leave the same trace: the
gateway's refusal, under the agent's name.
"""

from __future__ import annotations

from dataclasses import dataclass

from agentstack.observability.audit import AuditSink
from agentstack.observability.spans import SpanSink, Tracer, VersionStamp
from agentstack.policy.approval import ApprovalRecord, ApprovalStore
from agentstack.policy.approvers import (
    ApprovalReply,
    ApproverDirectory,
    ApproverNotAuthorized,
    authorize_approver,
)
from agentstack.runtime.run import Run, RunStore
from agentstack.runtime.waits import ResumeEvent, ResumeRejected, Wait, WaitStore, resume

# What an answer's audit record says was decided. It is recorded against the approval
# itself: the record is about the question and who answered it, not an effect on a system.
ANSWERED_ON = "approval"
APPROVED = "human.approved"
REFUSED = "human.refused"
NOT_AN_APPROVER = "approver.not_authorized"
NOT_APPLICABLE = "answer.not_applicable"


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
    audit: AuditSink
    # The answer is taken in the process that handles the click, which has no turn: its
    # span is exported from here, tagged with the run it answers.
    traces: SpanSink
    versions: VersionStamp

    def apply(self, reply: ApprovalReply) -> Resolution:
        run = self.runs.get(reply.run_id)
        if run is None:
            # No run, so no tenant to hold a record under: there is no one's account
            # this answer could be entered in. Refused, and the transport logged it.
            raise ReplyNotApplicable(
                f"{reply.run_id} is not a run; an answer to a question nobody asked"
            )

        # The tenant comes from the run. The interaction does not carry one, and if it
        # did it would not be consulted.
        try:
            approver = authorize_approver(
                self.directory, tenant=run.tenant, claimed_user_id=reply.slack_user_id
            )
        except ApproverNotAuthorized:
            # Still a principal: the claim, as a claim. Never promoted to a person.
            self._record(run, reply, f"slack:{reply.slack_user_id}", NOT_AN_APPROVER, "refused")
            raise

        wait = self.waits.get(reply.wait_id)
        refusal: str | None = None
        if wait is None or wait.run_id != reply.run_id:
            refusal = f"{reply.wait_id} is not a pending wait on {reply.run_id}"
        elif wait.withdrawn is not None:
            refusal = (
                f"{reply.wait_id} was withdrawn ({wait.withdrawn}); the world it asked "
                "about changed, so an answer to it is not a decision"
            )
        elif wait.satisfied:
            refusal = (
                f"{reply.wait_id} was already answered; the second reply to one "
                "question is not a second decision"
            )
        elif not wait.action_fingerprint or not wait.approval_summary:
            refusal = (
                f"{reply.wait_id} does not record what it was asking about, so no "
                "approval can be bound to it"
            )
        if refusal is not None:
            self._record(run, reply, approver.principal, NOT_APPLICABLE, "refused")
            raise ReplyNotApplicable(refusal)
        assert wait is not None and wait.action_fingerprint and wait.approval_summary

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
            self._record(run, reply, approver.principal, NOT_APPLICABLE, "refused")
            raise ReplyNotApplicable(str(exc)) from exc

        self._record(
            run,
            reply,
            approver.principal,
            APPROVED if reply.approved else REFUSED,
            "approved" if reply.approved else "refused",
            approval_id=None if record is None else record.id,
            wait=wait,
        )
        return Resolution(
            run_id=run.run_id,
            wait_id=wait.wait_id,
            approved=reply.approved,
            approver=approver.principal,
            approval_id=None if record is None else record.id,
        )

    def _record(
        self,
        run: Run,
        reply: ApprovalReply,
        principal: str,
        decision: str,
        outcome: str,
        *,
        approval_id: str | None = None,
        wait: Wait | None = None,
    ) -> None:
        """One answer, as an accountability record: who, about which wait, bound to which
        action and which state. Read from the wait, never from the reply: the reply names
        a wait and nothing it says about it is taken on trust."""
        tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=self.versions)
        with tracer.span("approval.answer", wait=reply.wait_id, decision=decision, outcome=outcome):
            pass
        self.traces.export(tracer.spans)
        if wait is None:
            found = self.waits.get(reply.wait_id)
            # A wait on another run is not this run's to describe.
            wait = found if found is not None and found.run_id == run.run_id else None
        self.audit.write(
            run_id=run.run_id,
            principal=principal,
            tenant=run.tenant,
            action_fingerprint=(wait.action_fingerprint if wait is not None else None) or "",
            surface=ANSWERED_ON,
            resource=reply.wait_id,
            policy_decision=decision,
            approval_id=approval_id,
            outcome=outcome,
            wait_id=reply.wait_id,
            state_snapshot=None if wait is None else wait.state_snapshot,
        )
