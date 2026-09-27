"""The bodies of the turn's nodes.

Lifted from `run_turn` unchanged in behaviour: the same calls in the same order, with
the early returns turned into statuses the graph reads on its edges. Everything a node
needs beyond plain state comes from `runtime.context`, which is not checkpointed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from langgraph.runtime import Runtime

from agentstack.context.assemble import ContextBundle
from agentstack.context.assemble import assemble as assemble_context
from agentstack.context.items import ContextItem, Scope, Trust
from agentstack.execution.surfaces import SurfaceRefused
from agentstack.model.contract import ExposedTool, ModelRequest
from agentstack.policy.approval import ApprovalRequired, ApprovalStale
from agentstack.policy.prompt import ApprovalPrompt
from agentstack.runtime.graph import TurnContext, TurnState
from agentstack.runtime.snapshot import resource_snapshot
from agentstack.tools.registry import ToolNotExposed
from agentstack.tools.validation import InvalidToolArguments


class WrongRun(RuntimeError):
    """The checkpoint and the context describe different runs."""


def _same_run(state: TurnState, ctx: TurnContext) -> str:
    """Every node checks that its state and its context agree about the run.

    State is checkpointed; the context is rebuilt per invocation. Once checkpoints are
    durable (T8b) those two come from different places and different processes, and a
    resume handed the wrong context would execute one run's graph with another run's
    gateway, envelope and tenant. Cheap to check, catastrophic to miss.
    """
    run_id = state.get("run_id")
    if run_id != ctx.run.run_id:
        raise WrongRun(
            f"checkpoint is for run {run_id!r}, context is for {ctx.run.run_id!r}; "
            "refusing to advance a run with another run's identity"
        )
    return ctx.run.run_id


def _exposed(ctx: TurnContext) -> Any:
    """The tools this run may use. Deterministic, so it is recomputed rather than
    carried across a resume."""
    return ctx.deps.registry.expose_for(tenant=ctx.run.tenant, stage=ctx.run.stage)


def check_waits(state: TurnState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """A run that is waiting is waiting.

    Satisfying the wait is what lets it continue - not the approval store having been
    written to by someone, somewhere.
    """
    ctx = runtime.context
    _same_run(state, ctx)
    run = ctx.run
    tracer = ctx.carried["tracer"]
    pending = ctx.deps.waits.pending_for(run.run_id)
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
        ctx.carried["pending_wait"] = pending[0]
        return {
            "status": "blocked",
            "blocked_on": [w.wait_id for w in pending],
            "text": (
                f"run {run.run_id} is waiting on {pending[0].kind}; satisfy the wait "
                "with a resume event before continuing"
            ),
        }

    resumed = ctx.deps.waits.satisfied_for(run.run_id)
    resumed_by = resumed[-1].payload.get("approved_by") if resumed else None
    with tracer.span("run.start", tenant=run.tenant, stage=run.stage, resumed_by=resumed_by):
        pass
    return {"status": "running", "resumed_by": resumed_by}


def assemble(state: TurnState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    _same_run(state, ctx)
    bundle = _assembled(state, ctx)
    return {"context_fingerprint": bundle.fingerprint()}


def _assembled(state: TurnState, ctx: TurnContext) -> ContextBundle:
    """Assemble the turn's context and keep it on this process's view of the turn.

    The bundle is never checkpointed, only its fingerprint. So a process that resumes a
    turn past `assemble` (after the one that assembled it died) builds it again here,
    from the checkpointed message and the stores.
    """
    run = ctx.run
    tracer = ctx.carried["tracer"]
    scope = Scope(tenant=run.tenant, user=run.user, session=run.session_id)
    latest = ContextItem(
        kind="user_message",
        text=state["message"],
        scope=scope,
        provenance="channel:inbound",
        observed_at=datetime.now(UTC),
        reason="the request this turn answers",
        trust=Trust.UNTRUSTED,
    )
    bundle = assemble_context(
        instructions=ctx.instructions,
        latest_message=latest,
        retrieved=ctx.deps.retriever.search(state["message"]),
        memories=ctx.deps.memory.recall(scope),
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
    ctx.carried["bundle"] = bundle
    return bundle


def expose(state: TurnState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    _same_run(state, ctx)
    exposed = ctx.deps.registry.expose_for(tenant=ctx.run.tenant, stage=ctx.run.stage)
    with ctx.carried["tracer"].span("tool.expose", tools=[s.name for s in exposed]):
        pass
    return {"exposed": [s.name for s in exposed]}


def call_model(state: TurnState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    _same_run(state, ctx)
    # Resumed here by a process that never assembled this turn: the one that did died
    # during the model call. Assemble again, and record what this call actually saw.
    rebuilt = "bundle" not in ctx.carried
    bundle = _assembled(state, ctx) if rebuilt else ctx.carried["bundle"]
    exposed = _exposed(ctx)
    response = ctx.deps.engine.generate(
        ModelRequest(
            instructions=ctx.instructions,
            rendered_context=bundle.render(),
            exposed_tools=tuple(
                ExposedTool(
                    name=spec.name,
                    description=spec.description,
                    parameters=spec.input_schema,
                )
                for spec in exposed
            ),
            max_output_tokens=ctx.deps.engine.asset.max_output_tokens,
        )
    )
    with ctx.carried["tracer"].span("model.call", proposals=[p.tool for p in response.proposals]):
        pass
    update: dict[str, Any] = {
        "proposals": [{"tool": p.tool, "arguments": dict(p.arguments)} for p in response.proposals],
        "model_text": response.text,
    }
    if rebuilt:
        update["context_fingerprint"] = bundle.fingerprint()
    return update


def act(state: TurnState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    """Prepare, gate and commit each proposal. The only node that reaches a surface."""
    ctx = runtime.context
    _same_run(state, ctx)
    run, deps, tracer = ctx.run, ctx.deps, ctx.carried["tracer"]
    # Recomputed, not carried. A resumed turn skips the nodes that produced these, so
    # anything `act` needs is either in state or derivable from the stores. Re-running
    # the exposure filter here is not a cost - it re-checks entitlement at the moment
    # the tool is actually used.
    exposed = _exposed(ctx)
    fingerprint = state["context_fingerprint"]

    receipts: list[str] = []
    observations: list[dict[str, Any]] = []
    refusals: list[str] = []
    # What this run has already changed, per resource.
    committed_against: dict[str, list[str]] = {}

    for proposal in state.get("proposals", []):
        tool = str(proposal["tool"])
        try:
            request = deps.registry.prepare(tool, proposal["arguments"], exposed=exposed)
        except (InvalidToolArguments, ToolNotExposed) as exc:
            # A malformed or unentitled proposal is a refusal, not a crash. It leaves
            # evidence and an answer; it never reaches a surface.
            with tracer.span("tool.reject", tool=tool, reason=str(exc)):
                pass
            refusals.append(str(exc))
            continue
        spec = deps.registry.spec(tool)
        state_snapshot = resource_snapshot(
            fingerprint, request.resource, committed_against.get(request.resource, [])
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
                    envelope=ctx.envelope,
                    run_id=run.run_id,
                    state_snapshot=state_snapshot,
                    tracer=tracer,
                )
                observations.append(read.data)
                continue
            # The step name carries the identity of the *action*, not just the tool.
            step_name = f"execute:{spec.name}:{request.fingerprint()}"
            with deps.steps.step(run.run_id, step_name) as slot:
                if slot[0] is None:
                    result = deps.gateway.execute(
                        request=request,
                        spec=spec,
                        envelope=ctx.envelope,
                        run_id=run.run_id,
                        state_snapshot=state_snapshot,
                        tracer=tracer,
                    )
                    slot[0] = result.receipt
                receipts.append(str(slot[0]))
                committed_against.setdefault(request.resource, []).append(str(slot[0]))
        except SurfaceRefused as exc:
            # The resource was not in the state this action needs, and nothing applied.
            # An answer, not a crash - the same as a proposal refused before the surface.
            with tracer.span("tool.reject", tool=spec.name, reason=str(exc)):
                pass
            refusals.append(str(exc))
            continue
        except (ApprovalRequired, ApprovalStale) as exc:
            summary = ApprovalPrompt(
                spec=spec,
                resource=request.resource,
                payload=request.payload,
                principal=ctx.envelope.principal,
                acts_as=ctx.envelope.acts_as,
                requested_by=run.user,
                channel=run.channel,
            ).render()
            wait = deps.waits.park(
                run_id=run.run_id,
                kind="human_approval",
                state_snapshot=state_snapshot,
                # What the wait is about, recorded now because the process that
                # handles the answer may not be this one.
                action_fingerprint=request.fingerprint(),
                approval_summary=summary,
            )
            with tracer.span("response", status="awaiting_approval", wait=wait.wait_id):
                pass
            ctx.carried["pending_wait"] = wait
            ctx.carried["pending_request"] = request
            ctx.carried["approval_summary"] = summary
            return {
                "status": "awaiting_approval",
                "text": str(exc),
                "wait_id": wait.wait_id,
                "receipts": receipts,
                "observations": observations,
                "refusals": refusals,
            }

    return {
        "status": "acted",
        "receipts": receipts,
        "observations": observations,
        "refusals": refusals,
    }


def respond(state: TurnState, runtime: Runtime[TurnContext]) -> dict[str, Any]:
    ctx = runtime.context
    _same_run(state, ctx)
    tracer = ctx.carried["tracer"]
    receipts = state.get("receipts", [])
    refusals = state.get("refusals", [])
    observations = state.get("observations", [])

    if refusals and not receipts and not observations:
        with tracer.span("response", status="rejected", refusals=len(refusals)):
            pass
        return {"status": "rejected", "text": "; ".join(refusals)}

    # Extraction and consolidation are maintenance. They do not block the turn.
    ctx.deps.maintenance.enqueue("extract_candidate_memories", ctx.run.run_id)
    with tracer.span("response", status="complete", receipts=len(receipts), refusals=len(refusals)):
        pass
    return {"status": "complete", "text": state.get("model_text", "")}
