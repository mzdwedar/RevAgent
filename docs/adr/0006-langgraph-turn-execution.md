# ADR-0006: The turn runs as a LangGraph graph

- Status: accepted
- Date: 2026-09-23
- Layer: 3 (runtime)
- Follows: ADR-0002 (durable execution backend)

## What was verified, not assumed

The plan marked T8a "verify first": the LangGraph API was assumed, not known. It was
checked against the installed package (`langgraph` 1.2.12, `langgraph-checkpoint` 4.2.0,
`langgraph-checkpoint-postgres` 3.1.2) rather than against memory, and two findings
changed the design.

### 1. The default durability is `"async"`

`invoke(..., durability=None)` resolves to `"async"` (`pregel/main.py`), which writes
checkpoints without waiting for them to land. Every durability claim this project makes
is about a process that *dies* - and a checkpoint that was still being written when the
process died is not a checkpoint.

**Every invocation passes `durability="sync"` explicitly**, and a test asserts it. The
cost is a synchronous write per step; the alternative is a guarantee that holds except
in the one case it exists for.

### 2. Code before `interrupt()` runs twice

Verified directly: a node that increments a counter, calls `interrupt()`, then
increments another, shows `{"before": 2, "after": 1}` across a pause and a resume. On
resume the node re-enters from the top and `interrupt()` returns the resume value.

This is the Part 4 failure mode with a new face. Any side effect placed before an
`interrupt()` in the same node happens twice. The two-phase idempotency ledger would
catch a repeated commit - but relying on the safety net to excuse the structure is how
the net ends up load-bearing.

**Rule: a node that interrupts does nothing before it interrupts.** This binds T17,
which is where the Slack approval actually meets the graph.

## What the graph owns, and what it does not

LangGraph owns *position*: which node is next, and the state that got there. It does not
become the record of anything.

- **Effects** stay in `run_steps` and `idempotency_claims` (T3, T4).
- **Waits** stay in the `waits` table. A checkpoint says where a turn stopped; the wait
  says what it is waiting for and what will satisfy it, and it is what T17's Slack
  callback resolves against.
- **Run identity** stays in `runs`. The graph thread carries `run_id`; it does not
  define it.

That split is why the fitness tests did not have to change: they assert invariants about
those stores, and the stores did not move.

## State is serialisable; dependencies are not

`TurnState` is a TypedDict of plain data - ids, status, receipts, refusals, fingerprints.
The gateway, registry, model engine and tracer travel in LangGraph's `context`, which is
passed per invocation and never checkpointed.

This is the decision T8b depends on. Had the tracer or the gateway gone into state, the
Postgres checkpointer would have to serialise a live database handle, and the answer
would be discovered at the point where it is most expensive to change. It is also
correct on its own: a run resumed in a new process should emit new spans, not replay the
dead process's tracer.

## One turn is one graph invocation

`run_turn` remains one turn. A run spans many turns, and that spanning is recorded in
`runs` and the transcript, not in a graph thread.

Threads are keyed `thread_id = run_id` with a per-turn `checkpoint_ns`, so run identity
is visible in the thread while turns stay isolated. Within a turn, a completed node is
not re-run when the same turn is invoked again - which is the property T10 will kill a
process to prove.

## Deferred

`interrupt()` is **not** adopted here. The current semantics are that a turn needing a
human returns `awaiting_approval` and the wait lives in Postgres; adopting in-graph
pausing changes the control flow, and T8a's acceptance says wait semantics are
unchanged. T17 decides it, with finding 2 above as the constraint it has to satisfy.

## T8b: where the checkpoints live

`PostgresSaver` has no schema parameter. It creates `checkpoints`, `checkpoint_blobs`,
`checkpoint_writes` and `checkpoint_migrations` wherever the connection's search_path
points, and tracks its own DDL in `checkpoint_migrations`.

Left at the default that is `public`: two migration ledgers in one namespace, neither
aware of the other, and `agentstack-migrate down --to 0` leaving four tables nobody can
account for. Copying the library's DDL into `migrations/` is worse - it couples this
repository to a vendor's internals and gives one set of tables two histories that can
disagree.

So `migrations/0004` creates a `langgraph` schema and the saver gets a pool of its own
with `search_path=langgraph`. We own the boundary; the library owns what is inside it.

Three properties of that pool are requirements rather than preferences:

- **autocommit** - `setup()` issues `CREATE INDEX CONCURRENTLY`, which Postgres refuses
  inside a transaction block. It also means a checkpoint write has landed when the call
  returns, which is what `durability="sync"` is for.
- **`row_factory=dict_row`** - the saver reads its rows as mappings. A tuple-row pool
  type-checks as a pool and fails at the first read.
- **`prepare_threshold=0`** - pooled connections are handed round, and server-side
  prepared statements bound to one of them are a known way to get "prepared statement
  does not exist" behind a pooler.

**Ordering.** The checkpointer looks like infrastructure that comes before migrations
and is the one piece that comes after: its schema is created by one. Postgres reports
this as "no schema has been selected to create in", which names neither the schema nor
the migration, so `open_checkpointer` checks first and says which command fixes it.
