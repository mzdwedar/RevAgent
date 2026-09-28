"""One turn, assembled from the layers below it.

The runtime's job is to advance the run: assemble context, call the model, interpret
the output, prepare tool requests, hand them to the execution gateway, park a wait
when a human is needed, and emit evidence at every one of those points. It owns
progress. It does not own authority, and it never touches a surface directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from agentstack.context.assemble import ContextBundle
from agentstack.context.memory import MaintenanceQueue, MemoryStore
from agentstack.context.retrieval import Retriever
from agentstack.execution.gateway import Gateway
from agentstack.model.engine import ModelEngine
from agentstack.observability.spans import Tracer, VersionStamp
from agentstack.policy.envelope import IdentityEnvelope
from agentstack.runtime.graph import TurnContext, advance, turn_thread
from agentstack.runtime.run import Run
from agentstack.runtime.steps import StepLedger
from agentstack.runtime.waits import Wait, WaitStore
from agentstack.tools.action import ActionRequest
from agentstack.tools.registry import Registry


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
    # The compiled turn, with whatever checkpointer the composition root chose.
    graph: Any


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
    turn_id: str | None = None,
    tracer: Tracer | None = None,
) -> TurnResult:
    """One turn, executed as a checkpointed graph (ADR-0006).

    The sequence and the behaviour are the ones this function always had; what changed
    is that each step is a node with a checkpoint after it, so a turn that dies halfway
    resumes instead of calling the model again to rediscover what it already said.

    `turn_id` names the checkpoint namespace. Left unset it is unique per call, which
    is the right default for a fresh turn; passing the same one twice resumes that turn.

    `tracer` is for a caller that must export the turn's spans however it ends: a turn
    that meets an effect of unknown outcome parks the run and raises `UnresolvedEffect`,
    and the spans that led there are the ones someone reconciling will want.
    """
    if tracer is None:
        tracer = Tracer(run_id=run.run_id, session_id=run.session_id, versions=deps.versions)
    context = TurnContext(
        run=run,
        envelope=envelope,
        deps=deps,
        instructions=instructions,
        carried={"tracer": tracer},
    )
    final = advance(
        deps.graph,
        turn_thread(run.run_id, turn_id),
        {"run_id": run.run_id, "message": message, "status": "starting"},
        context,
    )
    return _result(final, context, tracer)


def _result(final: dict[str, Any], context: TurnContext, tracer: Tracer) -> TurnResult:
    """Assemble the turn's answer from checkpointed state plus this process's view.

    The bundle and the tracer are not in state and are not meant to be: they are what
    *this* process did. A turn resumed elsewhere gets its own, which is correct - spans
    describe the process that emitted them.
    """
    empty = ContextBundle(instructions="", items=(), dropped_out_of_scope=0, dropped_over_budget=0)
    return TurnResult(
        run_id=final["run_id"],
        status=final.get("status", "complete"),
        text=final.get("text", ""),
        bundle=context.carried.get("bundle", empty),
        tracer=tracer,
        receipts=list(final.get("receipts", [])),
        observations=list(final.get("observations", [])),
        refusals=list(final.get("refusals", [])),
        pending_wait=context.carried.get("pending_wait"),
        pending_request=context.carried.get("pending_request"),
        approval_summary=context.carried.get("approval_summary"),
    )
