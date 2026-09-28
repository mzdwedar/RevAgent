"""The single choke point between "the model asked" and "the world changed".

Order matters here, and it is the order Part 7 argues for:

0. **Binding** - is this request the spec's own: its tool, surface, verb, fixed values?
1. **Policy** - is this permitted at all?
2. **Approval** - bound to this run, this action fingerprint, this state snapshot.
3. **Containment** - is it inside the sandbox even though it is allowed?
4. **Idempotency** - has this exact effect already been committed?
5. **Commit** - the surface does the thing.
6. **Evidence** - a span for debugging, an audit record for accountability.

Every terminal decision here is audited, not just the ones that succeed: a policy
denial, an approval refusal, a containment violation, a deduplicated repeat and a
commit all leave a record with the rule that produced them.

Reads and commits take the same first three steps and then diverge. `read` returns
data and touches no ledger; `execute` commits an effect and refuses anything that has
not declared itself side-effecting. The model cannot route around either, which is
the point: the executor, not the model, owns the boundary.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentstack.execution.idempotency import ClaimState, IdempotencyLedger
from agentstack.execution.surfaces import (
    Sandbox,
    SandboxViolation,
    SurfaceClient,
    SurfaceRefused,
    resource_verb,
)
from agentstack.observability.audit import AuditSink
from agentstack.observability.spans import Tracer
from agentstack.policy.approval import (
    ApprovalRecord,
    ApprovalRequired,
    ApprovalStale,
    ApprovalStore,
    require_approval,
)
from agentstack.policy.decisions import PolicyDenied, bound_to, decide
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.policy.precommit import PreCommitPolicy
from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import Surface, ToolSpec


class UnresolvedEffect(RuntimeError):
    """The effect may or may not have applied, and only the surface knows which.

    Raised instead of retrying. A retry here is the difference between one refund
    and two.
    """


@dataclass(frozen=True, slots=True)
class ExecutionResult:
    receipt: str
    deduplicated: bool
    approval_id: str | None


@dataclass(frozen=True, slots=True)
class ReadResult:
    """What a read returns. Not a receipt - a read that cannot answer is not a read."""

    data: dict[str, Any]
    approval_id: str | None


@dataclass(slots=True)
class Gateway:
    surfaces: dict[Surface, SurfaceClient]
    ledger: IdempotencyLedger
    approvals: ApprovalStore
    audit: AuditSink
    sandbox: Sandbox
    # The rule PRE_COMMIT actions are judged against. Defaulted rather than
    # required, because its own default is to refuse - a gateway constructed
    # without one grants nothing, which is the safe direction to be wrong in.
    pre_commit: PreCommitPolicy = field(default_factory=PreCommitPolicy)

    def read(
        self,
        *,
        request: ActionRequest,
        spec: ToolSpec,
        envelope: IdentityEnvelope,
        run_id: str,
        state_snapshot: str,
        tracer: Tracer,
    ) -> ReadResult:
        approval = self._authorize(
            request=request,
            spec=spec,
            envelope=envelope,
            run_id=run_id,
            state_snapshot=state_snapshot,
            tracer=tracer,
            writes=False,
        )
        client = self.surfaces[request.surface]
        data = client.read(request.resource, request.payload)
        with tracer.span(
            "execution.read",
            tool=spec.name,
            surface=request.surface.value,
            resource=request.resource,
        ):
            pass
        return ReadResult(data=data, approval_id=approval.id if approval else None)

    def execute(
        self,
        *,
        request: ActionRequest,
        spec: ToolSpec,
        envelope: IdentityEnvelope,
        run_id: str,
        state_snapshot: str,
        tracer: Tracer,
    ) -> ExecutionResult:
        if not spec.side_effecting:
            raise ValueError(
                f"{spec.name} is not side-effecting; use read(). Committing a read "
                "would put it in the effect ledger and audit it as a change."
            )
        approval = self._authorize(
            request=request,
            spec=spec,
            envelope=envelope,
            run_id=run_id,
            state_snapshot=state_snapshot,
            tracer=tracer,
            writes=True,
        )
        approval_id = approval.id if approval else None

        # Phase one: stake the key *before* anything can apply.
        claim = self.ledger.claim(request.idempotency_key)
        if claim.state is ClaimState.COMMITTED:
            with tracer.span("execution.commit", tool=spec.name, deduplicated=True):
                pass
            self._audit(
                request,
                spec,
                envelope,
                run_id,
                self._granting_rule(approval),
                approval_id,
                "deduplicated",
            )
            return ExecutionResult(
                receipt=str(claim.receipt), deduplicated=True, approval_id=approval_id
            )
        if claim.state is ClaimState.IN_FLIGHT:
            with tracer.span(
                "execution.commit",
                tool=spec.name,
                outcome="unresolved",
                claimed_at=str(claim.claimed_at),
            ):
                pass
            self._audit(
                request, spec, envelope, run_id, "effect.unresolved", approval_id, "unresolved"
            )
            raise UnresolvedEffect(
                f"{request.idempotency_key} was claimed and never settled: the effect "
                "may already have applied. Reconcile against the surface before "
                "retrying - an unfinalized claim is an unknown outcome, not a free slot."
            )

        client = self.surfaces[request.surface]
        try:
            receipt = client.commit(request.resource, request.payload)
        except SurfaceRefused:
            # The one failure the surface can prove did not apply. Release the key:
            # nothing needs reconciling, and the call that comes once the precondition
            # holds must not be blocked by this one. The ledger names this case as the
            # only caller permitted to abandon a claim.
            self.ledger.abandon(request.idempotency_key)
            self._audit(request, spec, envelope, run_id, "surface.refused", approval_id, "refused")
            raise
        except Exception as exc:
            # The claim deliberately stays. From here, "the surface refused" and "the
            # answer was lost" look identical, and only one of them is safe to retry.
            self._audit(
                request, spec, envelope, run_id, "effect.unresolved", approval_id, "unresolved"
            )
            raise UnresolvedEffect(
                f"{request.idempotency_key}: the surface did not confirm ({exc}). "
                "Reconcile against the surface; do not retry blind."
            ) from exc
        # Phase two: the surface answered, so the key can settle.
        self.ledger.finalize(request.idempotency_key, receipt)

        with tracer.span(
            "execution.commit",
            tool=spec.name,
            surface=request.surface.value,
            resource=request.resource,
            deduplicated=False,
        ):
            pass
        self._audit(
            request,
            spec,
            envelope,
            run_id,
            self._granting_rule(approval),
            approval_id,
            "committed",
        )
        return ExecutionResult(receipt=receipt, deduplicated=False, approval_id=approval_id)

    def _authorize(
        self,
        *,
        request: ActionRequest,
        spec: ToolSpec,
        envelope: IdentityEnvelope,
        run_id: str,
        state_snapshot: str,
        tracer: Tracer,
        writes: bool,
    ) -> ApprovalRecord | None:
        """Binding, then policy, then approval, then containment. Shared by both verbs.

        Binding first: every later step judges `spec` and trusts that `request` is one of
        its requests. A request prepared as a rollout and presented beside the abstention
        spec would otherwise be checked for the annotate scope, granted by the
        abstention's `PRE_COMMIT` rule, and committed at 100% with nobody asked (H2).
        The verb is read by the surface's own parser, for the act the caller is about to
        ask of it - a read and a write of the same resource are different verbs.
        """
        verb = resource_verb(request.surface, request.resource, writes=writes)
        decision = bound_to(spec, request, verb=verb) or decide(
            envelope=envelope, spec=spec, request=request
        )
        with tracer.span(
            "policy.decide", tool=spec.name, rule=decision.rule, allowed=decision.allowed
        ):
            pass
        if not decision.allowed:
            self._audit(request, spec, envelope, run_id, decision.rule, None, "denied")
            raise PolicyDenied(decision.reason)

        approval: ApprovalRecord | None = None
        with tracer.span("approval.request", tool=spec.name, tier=spec.approval.value):
            try:
                approval = require_approval(
                    store=self.approvals,
                    spec=spec,
                    request=request,
                    run_id=run_id,
                    state_snapshot=state_snapshot,
                    envelope=envelope,
                    policy=self.pre_commit,
                )
            except (ApprovalRequired, ApprovalStale) as exc:
                rule = (
                    "approval.required" if isinstance(exc, ApprovalRequired) else "approval.stale"
                )
                self._audit(request, spec, envelope, run_id, rule, None, "refused")
                raise

        with tracer.span("execution.contain", tool=spec.name, surface=request.surface.value):
            try:
                self.sandbox.check(surface=request.surface, resource=request.resource)
            except SandboxViolation:
                self._audit(
                    request,
                    spec,
                    envelope,
                    run_id,
                    "containment.violation",
                    approval.id if approval else None,
                    "contained",
                )
                raise
        if approval is not None and not approval.by_a_human:
            # Criterion 20: the audit record names the rule that granted it. Reachable
            # through `approval_id` either way, but an accountability record that makes
            # you follow a join to find out nobody was asked is not doing its job.
            with tracer.span("approval.policy", tool=spec.name, rule=approval.rule):
                pass
        return approval

    def _granting_rule(self, approval: ApprovalRecord | None) -> str:
        """What the audit trail records as the authorisation for this act."""
        if approval is not None and not approval.by_a_human:
            return f"approval.policy:{approval.rule}"
        return "allow"

    def _audit(
        self,
        request: ActionRequest,
        spec: ToolSpec,
        envelope: IdentityEnvelope,
        run_id: str,
        rule: str,
        approval_id: str | None,
        outcome: str,
    ) -> None:
        # A read that succeeded is not an accountability event. A *refusal* is, whatever
        # the verb: something reached the choke point and was stopped, and six weeks
        # from now that is the question someone will be asking.
        if not spec.side_effecting and outcome in {"committed", "deduplicated"}:
            return
        self.audit.write(
            run_id=run_id,
            principal=envelope.principal,
            tenant=envelope.tenant,
            action_fingerprint=request.fingerprint(),
            surface=request.surface.value,
            resource=request.resource,
            policy_decision=rule,
            approval_id=approval_id,
            outcome=outcome,
        )
