"""The turn, as a graph (Part 4).

`run_turn` was a straight line with early returns. It is now the same straight line
with the branches named, checkpointed after every node, so a turn that dies halfway
resumes rather than restarting - and so the model is not called a second time to
rediscover what it already said.

Two things this deliberately does not do, both recorded in ADR-0006:

* **It does not become the record.** Effects stay in `run_steps`, claims in
  `idempotency_claims`, waits in `waits`, identity in `runs`. A checkpoint says where a
  turn stopped; the wait says what it is waiting for. That split is why none of the
  fitness tests moved.
* **It does not use `interrupt()`.** A turn needing a human still returns
  `awaiting_approval` and the wait still lives in Postgres. Adopting in-graph pausing
  changes the control flow, and T17 is where that decision belongs - constrained by the
  verified fact that code before an `interrupt()` runs twice.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from agentstack.runtime.waits import NEEDS_MIGRATION

if TYPE_CHECKING:
    from agentstack.policy.envelope import IdentityEnvelope
    from agentstack.runtime.loop import TurnDeps
    from agentstack.runtime.run import Run

# LangGraph resolves `durability=None` to "async", which writes checkpoints without
# waiting for them to land. Every durability claim here is about a process that dies,
# and a checkpoint still being written when the process died is not a checkpoint.
DURABILITY = "sync"

# The shape of a checkpointed turn: the keys of `TurnState` and the nodes a pending
# checkpoint can name as `next`. Bump it when a change means an older checkpoint would
# be misread - a key renamed or retyped, a node renamed or removed - and say in
# COMPATIBLE_SCHEMA_VERSIONS which older shapes this code can still resume.
#
# A checkpoint with no version is not assumed to be version 1. It is noticed, never
# guessed (ADR-0005): the whole failure this exists for is code confidently resuming a
# shape it only thinks it recognises.
CHECKPOINT_SCHEMA_VERSION = 1
COMPATIBLE_SCHEMA_VERSIONS = frozenset({1})


class NeedsMigration(RuntimeError):
    """This turn's checkpoint was written in a shape this code does not understand.

    Raised after the run has been parked, never instead of parking it: the exception is
    this process failing loudly, the wait is what the next process and the operator see.
    """


class TurnState(TypedDict, total=False):
    """Plain data only.

    The gateway, registry, engine and tracer travel in `TurnContext`, which LangGraph
    passes per invocation and never checkpoints. Putting a live database handle in
    state would be discovered at T8b, which is the most expensive place to find it -
    and a run resumed in a new process should emit new spans anyway, not replay a dead
    process's tracer.
    """

    run_id: str
    # Which shape this checkpoint was written in. Set when the turn starts, so every
    # checkpoint the turn writes carries it.
    schema_version: int
    message: str
    status: str
    text: str
    receipts: list[str]
    refusals: list[str]
    observations: list[dict[str, Any]]
    exposed: list[str]
    # The model's answer, as data. This is the one part of a turn that cannot be
    # recomputed: asking again could return something else, and the run would then be
    # continuing from a decision it never actually made.
    proposals: list[dict[str, Any]]
    model_text: str
    # The bundle is deterministic given the message and the stores, so only its
    # fingerprint is kept - which is all `act` ever used it for.
    context_fingerprint: str
    blocked_on: list[str]
    resumed_by: str | None
    wait_id: str | None


@dataclass(slots=True)
class TurnContext:
    """Everything a node needs and nothing a checkpoint should hold."""

    run: Run
    envelope: IdentityEnvelope
    deps: TurnDeps
    instructions: str
    # Filled as the turn proceeds. Not checkpointed, and not meant to be: these are
    # this process's view of this turn.
    carried: dict[str, Any] = field(default_factory=dict)


def turn_thread(run_id: str, turn_id: str | None = None) -> dict[str, Any]:
    """The config identifying one turn of one run.

    Both ids go in `thread_id`. The obvious-looking alternative - run in `thread_id`,
    turn in `checkpoint_ns` - does not work: `checkpoint_ns` is LangGraph's *subgraph*
    namespace, and `get_state` on a thread that uses it for anything else raises
    "Subgraph not found". Verified, not assumed.

    Run identity is not weakened by this. It is a prefix here and authoritative in the
    `runs` table; the thread id is a lookup key, not the record.
    """
    return {"configurable": {"thread_id": f"{run_id}:{turn_id or uuid.uuid4()}"}}


def advance(
    compiled: Any, config: dict[str, Any], start: TurnState, context: TurnContext
) -> dict[str, Any]:
    """Run the turn, resuming it if a previous attempt left it unfinished.

    The distinction is LangGraph's and it is not guessable: `invoke(input, config)`
    starts the graph from the beginning *whatever the checkpoint says*, while
    `invoke(None, config)` resumes from it. A turn that died after the model call and
    was restarted with input would call the model again - which is the cost this whole
    task exists to avoid.
    """
    snapshot = compiled.get_state(config)
    if snapshot.values:
        _require_compatible(snapshot, config, context)
    pending = snapshot.next
    if pending:
        return dict(compiled.invoke(None, config, context=context, durability=DURABILITY))
    start = {**start, "schema_version": CHECKPOINT_SCHEMA_VERSION}
    return dict(compiled.invoke(start, config, context=context, durability=DURABILITY))


def _require_compatible(snapshot: Any, config: dict[str, Any], context: TurnContext) -> None:
    """Refuse to touch a checkpoint written in a shape this code does not understand.

    Checked for any existing checkpoint, not only an unfinished one: invoking a finished
    thread with new input merges it into the old values, so an old shape leaks into the
    new turn just as surely as by resuming it.

    The run is parked on a `needs_migration` wait, which blocks every other turn of the
    run through the ordinary wait gate and is what `operator stalled` reports. Its state
    snapshot names the checkpoint, so resuming it has to identify what was migrated.
    """
    found = snapshot.values.get("schema_version")
    if found in COMPATIBLE_SCHEMA_VERSIONS:
        return
    run_id = context.run.run_id
    thread = config["configurable"]["thread_id"]
    checkpoint = snapshot.config["configurable"].get("checkpoint_id", "?")
    parked_against = f"{thread}@{checkpoint}: schema v{found}"
    waits = context.deps.waits
    if not any(
        w.kind == NEEDS_MIGRATION and w.state_snapshot == parked_against
        for w in waits.pending_for(run_id)
    ):
        waits.park(run_id=run_id, kind=NEEDS_MIGRATION, state_snapshot=parked_against)
    readable = ", ".join(f"v{v}" for v in sorted(COMPATIBLE_SCHEMA_VERSIONS))
    raise NeedsMigration(
        f"run {run_id} stopped at a checkpoint written in schema v{found}; this code "
        f"resumes {readable}. The run is parked in {NEEDS_MIGRATION} rather than resumed "
        "into a shape it does not understand: migrate the checkpoint, then satisfy the wait."
    )


def checkpoint_shape() -> dict[str, Any]:
    """What a checkpoint written by this code looks like, as plain data.

    The keys of `TurnState` with their declared types, and the nodes a pending
    checkpoint can name as `next`. Edges are left out on purpose: a checkpoint resumes
    at the node it names, so rewiring what comes after strands nothing. This is what
    `scripts/checkpoint_guard.py` compares against the last release (criterion 22).
    """
    from langgraph.checkpoint.memory import InMemorySaver

    nodes = build_turn_graph(InMemorySaver()).get_graph().nodes
    return {
        "version": CHECKPOINT_SCHEMA_VERSION,
        # Postponed annotations reach a TypedDict as ForwardRefs; the text written in
        # the source is the stable thing to compare.
        "fields": {
            name: str(getattr(kind, "__forward_arg__", kind))
            for name, kind in sorted(TurnState.__annotations__.items())
        },
        "nodes": sorted(n for n in nodes if not n.startswith("__")),
    }


def build_turn_graph(checkpointer: Any) -> Any:
    """Compile the turn against a checkpointer.

    The checkpointer is passed in rather than chosen here: whether a turn survives
    the process is a deployment decision, it is made once in the composition root,
    and a module-level default would be the in-memory one - durable-looking code
    silently backed by a dictionary.

    Nodes are imported inside the function because they import this module back for
    their state and context types.
    """
    from agentstack.runtime import nodes

    graph: Any = StateGraph(TurnState, context_schema=TurnContext)
    graph.add_node("check_waits", nodes.check_waits)
    graph.add_node("assemble", nodes.assemble)
    graph.add_node("expose", nodes.expose)
    graph.add_node("call_model", nodes.call_model)
    graph.add_node("act", nodes.act)
    graph.add_node("respond", nodes.respond)

    graph.add_edge(START, "check_waits")
    graph.add_conditional_edges(
        "check_waits",
        # A run with an unsatisfied wait does not get to look at anything else. This
        # is the Part 3 gate, now an edge rather than an early return.
        lambda state: END if state.get("status") == "blocked" else "assemble",
        {END: END, "assemble": "assemble"},
    )
    graph.add_edge("assemble", "expose")
    graph.add_edge("expose", "call_model")
    graph.add_edge("call_model", "act")
    graph.add_conditional_edges(
        "act",
        lambda state: END if state.get("status") == "awaiting_approval" else "respond",
        {END: END, "respond": "respond"},
    )
    graph.add_edge("respond", END)
    return graph.compile(checkpointer=checkpointer)
