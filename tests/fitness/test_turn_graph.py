"""Part 4: the turn is a checkpointed graph, and the checkpoint has to be real.

Two of these exist because the LangGraph defaults are wrong for this system, which was
found by reading the package rather than by trusting it (ADR-0006): checkpoints are
written asynchronously unless told otherwise, and code before an `interrupt()` runs
twice.
"""

from __future__ import annotations

import inspect
import json
from typing import Any

import pytest

from agentstack.interfaces.inbound import InboundEvent
from agentstack.interfaces.wiring import Stack, build_stack, envelope_for, handle
from agentstack.runtime import graph, nodes
from agentstack.runtime.graph import TurnContext, TurnState
from agentstack.runtime.loop import run_turn
from agentstack.runtime.nodes import WrongRun
from agentstack.runtime.run import Run, new_run
from agentstack.storage.database import Database
from agentstack.tools.spec import Surface

from .conftest import SCOPES


def test_checkpoints_are_written_synchronously() -> None:
    """LangGraph resolves `durability=None` to "async", which does not wait for the
    write. Every durability claim here is about a process that died, and a checkpoint
    still in flight when it died is not a checkpoint."""
    assert graph.DURABILITY == "sync"
    assert "durability=DURABILITY" in inspect.getsource(graph.advance)


def test_the_thread_carries_the_run_and_the_turn() -> None:
    config = graph.turn_thread("run-7", "turn-1")

    assert config["configurable"]["thread_id"] == "run-7:turn-1"


def test_the_turn_is_not_put_in_the_subgraph_namespace() -> None:
    """`checkpoint_ns` is LangGraph's subgraph namespace. Using it to separate turns
    looks tidy and makes `get_state` raise "Subgraph not found" - found by trying it."""
    assert "checkpoint_ns" not in graph.turn_thread("run-7", "turn-1")["configurable"]


def test_turns_of_one_run_do_not_share_a_checkpoint() -> None:
    first = graph.turn_thread("run-7")["configurable"]["thread_id"]
    second = graph.turn_thread("run-7")["configurable"]["thread_id"]

    assert first != second
    assert first.startswith("run-7:") and second.startswith("run-7:")


class CountingEngine:
    """Wraps the real engine and records how often it was asked.

    An object rather than a patched method: assigning over `engine.generate` needs a
    type suppression, and the floor bans those for a reason - a fake that cannot be
    typed is usually a fake that does not match the contract.
    """

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.asset = inner.asset
        self.calls = 0

    def generate(self, request: Any) -> Any:
        self.calls += 1
        return self.inner.generate(request)


class RefusingClient:
    """A surface that cannot be reached. Stands in for the process dying mid-turn."""

    def __init__(self, inner: Any) -> None:
        self.inner = inner
        self.reachable = False

    def read(self, resource: str, query: dict[str, Any]) -> dict[str, Any]:
        if not self.reachable:
            raise RuntimeError("the process died here")
        return self.inner.read(resource, query)

    def commit(self, resource: str, payload: dict[str, Any]) -> str:
        return self.inner.commit(resource, payload)


def test_a_turn_that_died_resumes_without_re_running_what_it_finished(
    stack: Stack, run: Run
) -> None:
    """The point of checkpointing a turn.

    The model call is the expensive, non-deterministic part. A turn that died after it
    must not ask again - the second answer could be different, and the run would be
    continuing from a decision it never actually made.
    """
    engine = CountingEngine(stack.deps.engine)
    stack.deps.engine = engine

    # The surface is unreachable, so `act` dies after the model has already answered.
    surface = RefusingClient(stack.client)
    stack.deps.gateway.surfaces[Surface.API] = surface
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    envelope = envelope_for(view, scopes=SCOPES)
    message = "lookup_subscription tenant=acme customer_id=c-42"

    with pytest.raises(RuntimeError, match="the process died here"):
        run_turn(run=run, envelope=envelope, message=message, deps=stack.deps, turn_id="t1")
    assert engine.calls == 1

    surface.reachable = True
    result = run_turn(run=run, envelope=envelope, message=message, deps=stack.deps, turn_id="t1")

    assert result.status == "complete"
    assert engine.calls == 1, "the resumed turn called the model again for an answer it had"


def test_restarting_with_input_would_have_re_run_it() -> None:
    """Why `advance` checks for a pending node instead of always passing input.

    `invoke(input, config)` starts from the beginning whatever the checkpoint holds;
    only `invoke(None, config)` resumes. The distinction is not guessable from the
    signature, and getting it wrong costs a second model call per recovery.
    """
    source = inspect.getsource(graph.advance)

    assert "compiled.invoke(None, config" in source
    assert "get_state(config).next" in source


def test_a_fresh_turn_does_run_the_model(stack: Stack, run: Run) -> None:
    """The counterpart: the check above must not be passing because nothing runs."""
    engine = CountingEngine(stack.deps.engine)
    stack.deps.engine = engine
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    envelope = envelope_for(view, scopes=SCOPES)

    run_turn(run=run, envelope=envelope, message="hi", deps=stack.deps, turn_id="turn-a")
    run_turn(run=run, envelope=envelope, message="hi", deps=stack.deps, turn_id="turn-b")

    assert engine.calls == 2


def test_a_node_refuses_a_context_describing_a_different_run(stack: Stack, run: Run) -> None:
    """State is checkpointed and the context is rebuilt per invocation. Once those come
    from different processes, a resume handed the wrong context would drive one run's
    graph with another run's gateway, envelope and tenant."""
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    other = new_run(session_id=run.session_id, tenant=run.tenant, user=run.user, channel="test")
    context = TurnContext(
        run=other,
        envelope=envelope_for(view, scopes=SCOPES),
        deps=stack.deps,
        instructions="x",
    )
    state: TurnState = {"run_id": run.run_id, "message": "m"}

    with pytest.raises(WrongRun, match="another run's identity"):
        nodes._same_run(state, context)

    assert nodes._same_run({"run_id": other.run_id}, context) == other.run_id


def test_every_node_checks_the_run_it_was_handed() -> None:
    """The guard is only worth having if no node forgets it."""
    source = inspect.getsource(nodes)
    defined = [n for n in dir(nodes) if not n.startswith("_") and callable(getattr(nodes, n))]
    node_names = [n for n in defined if f"def {n}(state: TurnState" in source]

    assert len(node_names) == 6
    for name in node_names:
        body = inspect.getsource(getattr(nodes, name))
        assert "_same_run(state, ctx)" in body, f"{name} does not check its run"


def test_an_unsatisfied_wait_never_reaches_the_model(
    stack: Stack, event: InboundEvent, run: Run
) -> None:
    """The Part 3 gate, now a graph edge rather than an early return."""
    first = handle(stack, event, scopes=SCOPES, run=run)
    assert first.status == "awaiting_approval"

    engine = CountingEngine(stack.deps.engine)
    stack.deps.engine = engine

    second = handle(stack, event, scopes=SCOPES, run=run)

    assert second.status == "blocked"
    assert engine.calls == 0, "a blocked run assembled context and called the model anyway"


def test_the_checkpointed_state_holds_no_live_objects(stack: Stack, run: Run) -> None:
    """The decision T8b depends on. A gateway or a tracer in state would mean the
    Postgres checkpointer has to serialise a database handle, discovered at the most
    expensive moment to change it."""
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    run_turn(
        run=run,
        envelope=envelope_for(view, scopes=SCOPES),
        message="lookup_subscription tenant=acme customer_id=c-42",
        deps=stack.deps,
        turn_id="serialisable",
    )

    snapshot = stack.deps.graph.get_state(graph.turn_thread(run.run_id, "serialisable"))

    json.dumps(snapshot.values)  # raises if anything in state is not plain data


def test_the_context_carries_the_dependencies_not_the_state() -> None:
    """Stated as a test because the split is the whole reason T8b is tractable."""
    assert set(TurnContext.__dataclass_fields__) == {
        "run",
        "envelope",
        "deps",
        "instructions",
        "carried",
    }
    assert "gateway" not in TurnState.__annotations__
    assert "tracer" not in TurnState.__annotations__


# --- T8b: the checkpoint is a row, not a dictionary ---


def test_checkpoints_are_written_to_postgres(
    stack: Stack, run: Run, app_database: Database
) -> None:
    """The claim `InMemorySaver` could not make."""
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    run_turn(
        run=run,
        envelope=envelope_for(view, scopes=SCOPES),
        message="lookup_subscription tenant=acme customer_id=c-42",
        deps=stack.deps,
        turn_id="stored",
    )

    rows = app_database.fetch_all(
        "SELECT count(*) FROM langgraph.checkpoints WHERE thread_id = %s",
        (f"{run.run_id}:stored",),
    )

    assert rows[0][0] > 0, "the turn ran but left no checkpoint in Postgres"


def test_a_turn_resumes_from_storage_in_a_stack_that_never_saw_it(
    stack: Stack, run: Run, app_database: Database, app_database_url: str
) -> None:
    """The acceptance: resumed from storage, not from memory.

    The replacement stack gets its **own** checkpointer - a separate pool and a separate
    saver object - so the only thing the two share is the database. Reusing the session
    checkpointer would have passed with an in-memory saver too, which is how this test
    was wrong the first time: it proved the object was shared, not that anything was
    stored.
    """
    from agentstack.storage.checkpoints import open_checkpointer

    engine = CountingEngine(stack.deps.engine)
    stack.deps.engine = engine
    surface = RefusingClient(stack.client)
    stack.deps.gateway.surfaces[Surface.API] = surface
    view = stack.resolver.resolve(session_id=run.session_id, user_id=run.user, tenant=run.tenant)
    envelope = envelope_for(view, scopes=SCOPES)
    message = "lookup_subscription tenant=acme customer_id=c-42"

    with pytest.raises(RuntimeError, match="the process died here"):
        run_turn(run=run, envelope=envelope, message=message, deps=stack.deps, turn_id="t2")
    assert engine.calls == 1

    pool, fresh_saver = open_checkpointer(app_database_url)
    try:
        restarted = build_stack(app_database, fresh_saver, tenant=run.tenant)
        second = CountingEngine(restarted.deps.engine)
        restarted.deps.engine = second

        result = run_turn(
            run=run, envelope=envelope, message=message, deps=restarted.deps, turn_id="t2"
        )
    finally:
        pool.close()

    assert result.status == "complete"
    assert second.calls == 0, (
        "the replacement process called the model again; it resumed from nothing"
    )


def test_the_checkpoint_tables_are_not_in_our_schema(app_database: Database) -> None:
    """LangGraph creates and versions its own tables, in a schema of its own.

    Left in `public` they sit beside our tables with a second migration ledger
    (`checkpoint_migrations`) that our runner knows nothing about, and
    `migrate down --to 0` leaves four tables nobody can account for.
    """
    rows = app_database.fetch_all(
        "SELECT schemaname, tablename FROM pg_tables"
        " WHERE schemaname IN ('public', 'langgraph') ORDER BY tablename"
    )
    placed = {table: schema for schema, table in rows}

    for table in ("checkpoints", "checkpoint_blobs", "checkpoint_writes", "checkpoint_migrations"):
        assert placed.get(table) == "langgraph", f"{table} escaped into {placed.get(table)}"
    for table in ("runs", "waits", "approvals", "schema_migrations"):
        assert placed.get(table) == "public"


def test_the_checkpoint_pool_commits_as_it_goes() -> None:
    """A checkpoint still inside an open transaction when the process dies is not a
    checkpoint. `setup()` also needs it: CREATE INDEX CONCURRENTLY is refused inside a
    transaction block, which is how this was found."""
    from agentstack.storage import checkpoints

    source = inspect.getsource(checkpoints.open_checkpointer)

    assert '"autocommit": True' in source
    assert '"row_factory": dict_row' in source
    assert checkpoints.CHECKPOINT_SCHEMA == "langgraph"
    assert "search_path=langgraph" in checkpoints.checkpoint_url("postgresql://h/d")


def test_opening_a_checkpointer_before_migrating_says_so(app_database_url: str) -> None:
    """Postgres answers "no schema has been selected to create in", which names neither
    the schema nor the migration that makes it. The checkpointer looks like
    infrastructure that comes before migrations and is the one piece that comes after."""
    from agentstack.storage import checkpoints
    from agentstack.storage.provision import rebuild_database

    url = rebuild_database(app_database_url, "checkpoint_order_test")

    with pytest.raises(checkpoints.CheckpointSchemaMissing, match="agentstack-migrate up"):
        checkpoints.open_checkpointer(url)
