"""The single choke point between "the model asked" and "the world changed".

Order matters here, and it is the order Part 7 argues for:

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

from dataclasses import dataclass
from typing import Any

from agentstack.execution.idempotency import IdempotencyLedger
from agentstack.execution.surfaces import (
    Sandbox,
    SandboxViolation,
    SurfaceClient,
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
from agentstack.policy.decisions import PolicyDenied, decide
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.tools.action import ActionRequest
from agentstack.tools.spec import Surface, ToolSpec


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
        )
        approval_id = approval.id if approval else None

        recorded = self.ledger.recorded(request.idempotency_key)
        if recorded is not None:
            with tracer.span("execution.commit", tool=spec.name, deduplicated=True):
                pass
            self._audit(request, spec, envelope, run_id, "allow", approval_id, "deduplicated")
            return ExecutionResult(receipt=recorded, deduplicated=True, approval_id=approval_id)

        client = self.surfaces[request.surface]
        receipt = client.commit(request.resource, request.payload)
        self.ledger.record(request.idempotency_key, receipt)

        with tracer.span(
            "execution.commit",
            tool=spec.name,
            surface=request.surface.value,
            resource=request.resource,
            deduplicated=False,
        ):
            pass
        self._audit(request, spec, envelope, run_id, "allow", approval_id, "committed")
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
    ) -> ApprovalRecord | None:
        """Policy, then approval, then containment. Shared by both verbs."""
        decision = decide(envelope=envelope, spec=spec, request=request)
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
        return approval

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
