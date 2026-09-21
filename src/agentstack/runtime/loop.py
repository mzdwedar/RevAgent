"""One turn, assembled from the layers below it.

The runtime's job is to advance the run: assemble context, call the model, interpret
the output, prepare tool requests, hand them to the execution gateway, park a wait
when a human is needed, and emit evidence at every one of those points. It owns
progress. It does not own authority, and it never touches a surface directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agentstack.context.assemble import ContextBundle, assemble
from agentstack.context.items import ContextItem, Scope, Trust
from agentstack.context.memory import MaintenanceQueue, MemoryStore
from agentstack.context.retrieval import Retriever
from agentstack.execution.gateway import Gateway
from agentstack.model.contract import ModelRequest
from agentstack.model.engine import ModelEngine
from agentstack.observability.spans import Tracer, VersionStamp
from agentstack.policy.approval import ApprovalRequired, ApprovalStale
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.policy.prompt import ApprovalPrompt
from agentstack.runtime.run import Run
from agentstack.runtime.snapshot import resource_snapshot
from agentstack.runtime.steps import StepLedger
from agentstack.runtime.waits import Wait, WaitStore
from agentstack.tools.action import ActionRequest
from agentstack.tools.registry import Registry, ToolNotExposed
from agentstack.tools.validation import InvalidToolArguments


@dataclass(slots=True)
class TurnDeps:
    engine: ModelEngine
    registry: Registry
    gateway: Gateway
    retriever: Retriever
    memory: MemoryStore
    maintenance: MaintenanceQueue
    steps: StepLedger
    waits: WaitStore
    versions: VersionStamp


@dataclass(slots=True)
class TurnResult:
    run_id: str
    status: str
    text: str
    bundle: ContextBundle
    tracer: Tracer
    receipts: list[str] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    refusals: list[str] = field(default_factory=list)
    pending_wait: Wait | None = None
    pending_request: ActionRequest | None = None
    approval_summary: str | None = None


def run_turn(
    *,
    run: Run,
    envelope: IdentityEnvelope,
    message: str,
    deps: TurnDeps,
    instructions: str = "You are a support agent. Prefer the narrowest tool that fits.",
) -> TurnResult:
    tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=deps.versions)

    # A run that is waiting is waiting. Satisfying the wait is what lets it continue -
    # not the approval store having been written to by someone, somewhere.
    pending = deps.waits.pending_for(run.run_id)
    if pending:
        with tracer.span(
            "run.start",
            tenant=run.tenant,
            stage=run.stage,
            blocked_on=[w.wait_id for w in pending],
        ):
            pass
        with tracer.span("response", status="blocked", waits=len(pending)):
            pass
        return TurnResult(
            run_id=run.run_id,
            status="blocked",
            text=(
                f"run {run.run_id} is waiting on {pending[0].kind}; satisfy the wait "
                "with a resume event before continuing"
            ),
            bundle=ContextBundle(
                instructions="", items=(), dropped_out_of_scope=0, dropped_over_budget=0
            ),
            tracer=tracer,
            pending_wait=pending[0],
        )

    resumed = deps.waits.satisfied_for(run.run_id)
    resumed_by = resumed[-1].payload.get("approved_by") if resumed else None
    with tracer.span("run.start", tenant=run.tenant, stage=run.stage, resumed_by=resumed_by):
        pass

    scope = Scope(tenant=run.tenant, user=run.user, session=run.session_id)
    latest = ContextItem(
        kind="user_message",
        text=message,
        scope=scope,
        provenance="channel:inbound",
        observed_at=datetime.now(UTC),
        reason="the request this turn answers",
        trust=Trust.UNTRUSTED,
    )

    bundle = assemble(
        instructions=instructions,
        latest_message=latest,
        retrieved=deps.retriever.search(message),
        memories=deps.memory.recall(scope),
        requester_scope=scope,
    )
    with tracer.span(
        "context.assemble",
        fingerprint=bundle.fingerprint(),
        items=len(bundle.items),
        untrusted=len(bundle.untrusted()),
        dropped_out_of_scope=bundle.dropped_out_of_scope,
    ):
        pass

    exposed = deps.registry.expose_for(tenant=run.tenant, stage=run.stage)
    with tracer.span("tool.expose", tools=[s.name for s in exposed]):
        pass

    response = deps.engine.generate(
        ModelRequest(
            instructions=instructions,
            rendered_context=bundle.render(),
            exposed_tools=tuple(s.name for s in exposed),
            max_output_tokens=deps.engine.asset.max_output_tokens,
        )
    )
    with tracer.span("model.call", proposals=[p.tool for p in response.proposals]):
        pass

    receipts: list[str] = []
    # What this run has already changed, per resource. The input `resource_state`
    # will eventually come from the surface (ADR pending in SPEC.md); this is the
    # part the runtime can know on its own.
    committed_against: dict[str, list[str]] = {}
    observations: list[dict[str, Any]] = []

    refusals: list[str] = []

    for proposal in response.proposals:
        try:
            request = deps.registry.prepare(proposal.tool, proposal.arguments, exposed=exposed)
        except (InvalidToolArguments, ToolNotExposed) as exc:
            # A malformed or unentitled proposal is a refusal, not a crash. It leaves
            # evidence and an answer; it never reaches a surface.
            with tracer.span("tool.reject", tool=proposal.tool, reason=str(exc)):
                pass
            refusals.append(str(exc))
            continue
        spec = deps.registry.spec(proposal.tool)
        state_snapshot = resource_snapshot(
            bundle.fingerprint(),
            request.resource,
            committed_against.get(request.resource, []),
        )
        with tracer.span("tool.call", tool=spec.name, resource=request.resource):
            pass
        try:
            if not spec.side_effecting:
                # A read needs no step boundary: there is no effect to record, and
                # nothing to avoid repeating.
                read = deps.gateway.read(
                    request=request,
                    spec=spec,
                    envelope=envelope,
                    run_id=run.run_id,
                    state_snapshot=state_snapshot,
                    tracer=tracer,
                )
                observations.append(read.data)
                continue
            # The step name carries the identity of the *action*, not just the tool.
            # Two refunds on two charges in one turn are two steps; the same refund
            # retried is one. The action fingerprint already draws that line.
            step_name = f"execute:{spec.name}:{request.fingerprint()}"
            with deps.steps.step(run.run_id, step_name) as slot:
                if slot[0] is None:
                    result = deps.gateway.execute(
                        request=request,
                        spec=spec,
                        envelope=envelope,
                        run_id=run.run_id,
                        state_snapshot=state_snapshot,
                        tracer=tracer,
                    )
                    slot[0] = result.receipt
                receipts.append(str(slot[0]))
                committed_against.setdefault(request.resource, []).append(str(slot[0]))
        except (ApprovalRequired, ApprovalStale) as exc:
            wait = deps.waits.park(
                run_id=run.run_id, kind="human_approval", state_snapshot=state_snapshot
            )
            with tracer.span("response", status="awaiting_approval", wait=wait.wait_id):
                pass
            return TurnResult(
                run_id=run.run_id,
                status="awaiting_approval",
                text=str(exc),
                bundle=bundle,
                tracer=tracer,
                pending_wait=wait,
                pending_request=request,
                approval_summary=ApprovalPrompt(
                    spec=spec,
                    resource=request.resource,
                    payload=request.payload,
                    principal=envelope.principal,
                    acts_as=envelope.acts_as,
                    requested_by=run.user,
                    channel=run.channel,
                ).render(),
            )

    if refusals and not receipts and not observations:
        with tracer.span("response", status="rejected", refusals=len(refusals)):
            pass
        return TurnResult(
            run_id=run.run_id,
            status="rejected",
            text="; ".join(refusals),
            bundle=bundle,
            tracer=tracer,
            refusals=refusals,
        )

    # Extraction and consolidation are maintenance. They do not block the turn.
    deps.maintenance.enqueue("extract_candidate_memories", run.run_id)

    with tracer.span("response", status="complete", receipts=len(receipts), refusals=len(refusals)):
        pass
    return TurnResult(
        run_id=run.run_id,
        status="complete",
        text=response.text,
        bundle=bundle,
        tracer=tracer,
        receipts=receipts,
        observations=observations,
        refusals=refusals,
    )
