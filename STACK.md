# STACK.md — layer ledger

Derived from *The Agent Stack*, Parts 1–8. This file exists so that when something
breaks, the question **"which layer failed?"** has an answer instead of "the agent
hallucinated".

Read this before changing code. Name the layer(s) your change touches. If a change
needs two layers to become one, that is an architecture decision (write an ADR), not
a convenience.

## The layers

| # | Layer | Owner module | Authoritative store | Invariants | Status |
|---|---|---|---|---|---|
| 1 | Interfaces & channels | `agentstack.interfaces` | — | Entry points carry an event; they never resolve identity, policy or state themselves. Nothing imports this layer — which is why `TriggerEvent` lives in layer 8 beside the authority rule that reads it, while the parsing of an untrusted payload stays here. An unknown trigger kind is refused rather than defaulted: a default hands a typo whichever authority the default carries. The tenant on a trigger is a claim, tested in layer 8. | enforced |
| 2 | Control plane & session ownership | `agentstack.control_plane` | Postgres: `sessions`, `transcript_events`, `working_state` | `session_id` is never the user id — held by the constructor *and* by a CHECK constraint, so a path that skips the constructor still cannot do it. Transcript, working state and memory are three distinct stores, and three distinct tables. The runtime receives a bounded `SessionView`, never the raw record. State outlives the process: a stack rebuilt on the same database resolves the same session. | enforced |
| 3 | Runtime, workflows, durable execution | `agentstack.runtime` | Postgres: `runs`, `run_steps`, `waits` (graph backend: ADR-0002) | Every run has a stable `run_id`, and it is a row — a step or a wait keyed on a run nothing recorded is an orphan. Side effects happen only inside a recorded step, and `run_steps_complete_once` means the database refuses a second completion rather than trusting the check that precedes it. Waiting is persisted state, not a sleeping process: a wait outlives the process that parked it, an unsatisfied wait blocks the run, and two resumes racing produce one winner. Replay rebuilds state; it never re-rolls non-determinism — the turn is a checkpointed LangGraph graph ([ADR-0006](docs/adr/0006-langgraph-turn-execution.md)), written synchronously, and a turn that died resumes without asking the model again. The graph owns position only: effects, waits and run identity stay in Postgres. | enforced |
| 4 | Model engine & inference | `agentstack.model` | — | Model asset, serving system and interaction contract are three separate choices, recorded separately (ADR-0003). Tool calls that come back are **proposals**, not executions. Compatible is not equivalent. | enforced |
| 4b | Tabular inference (churn) | `agentstack.prediction` | `data/scores/*.json` (recorded); the weights are PriorLabs' TabPFN-3.5, open under a **non-commercial** licence accepted once per machine — open to read and run, not to ship commercially, which constrains deploying this as the production system it is shaped like | A second layer-4 engine, and a **separate package on purpose**: TabPFN and the language model are both "models" in English and share no asset, serving system, licence or failure mode, so one package holding both would make "the model version" mean two things in an experiment's provenance. The licence gate refuses at startup, never mid-run (criterion 18). Scores are out of fold — a row is never scored by a model that saw its label. The scorer returns a probability and decides nothing: the targeting predicate is policy. | enforced |
| 5 | Context, retrieval, memory | `agentstack.context` | retrieval index; cohort snapshots on disk + `data/manifest.json`; `MemoryStore` **still process-local** (Checkpoint A gap, `tasks/todo.md`) | Prompt context is a derived, inspectable view — never the canonical record. Every item carries scope, provenance, freshness, reason and trust. Memory writes are explicit. Maintenance runs off the hot path. Retrieval returns candidates, not authority. A cohort is identified by its contents: `data_as_of` is a content hash, never a clock, so "the same snapshot" is a checkable claim. No column is dropped without a recorded reason. Targeting is deterministic and frozen into `experiment_version` — same snapshot and model version, same customers — and membership stays re-checkable so a control subject can be shown to have passed the same predicate. A cohort that misses a gate is refused, never widened. | enforced |
| 6 | Tools, MCP, capability surfaces | `agentstack.tools` | tool registry | A schema validates shape; it never grants authority. Tools are narrow and intent-specific. Exposure is filtered per run. Tool output is an untrusted observation. | enforced |
| 7 | Execution surfaces | `agentstack.execution` | Postgres: `idempotency_claims` | Every action names its surface. `gateway.execute` is the only path to a real client. Every side effect carries an identity envelope and an idempotency key. The claim is one atomic upsert, so of two runs reaching the same key together exactly one may act; an unresolved claim survives the process that made it, which is the only process that could have known. | enforced |
| 8 | Identity, trust, policy, approvals | `agentstack.policy` | Postgres: `approvals` | Approval sits immediately before the irreversible act and binds run id, action fingerprint, state snapshot and approver, and outlives the process that recorded it — a human is asked once. A blank approver or summary is refused by `grant` and again by a CHECK. Untrusted content never increases available authority. Three tiers, three behaviours: `NONE` checks nothing, `PRE_COMMIT` grants on a named rule that can refuse and records which rule granted it, `ALWAYS` wakes a person — and a policy grant never satisfies `ALWAYS`, or the strongest tier would be the easiest to mint. | enforced |
| 9 | Observability, evaluation, feedback | `agentstack.observability` | trace sink **and a separate** audit sink: Postgres schema `audit` | Traces cross the whole stack, not just the model call. Audit records are not debug logs: they live in their own schema so access and retention can differ, and they carry **no** foreign key onto `runs` — deleting the session deletes the operational record and leaves the accountability record standing. Evaluation judges the path, not only the answer. A failure is not closed until it is a regression case. | enforced |
| 10 | Infrastructure substrate | `agentstack.storage` | Postgres (`docker-compose.yml` in dev) | Mostly inherited: delivery, consistency, isolation and failure semantics are recorded as assumptions in `SPEC.md` (Foundation Assumptions) and must be stated before the runtime depends on them. What *is* written is the seam onto it — one pooled connection factory, one migration runner, and the rule that only this package holds the driver (`lint-imports` contract 5, [ADR-0005](docs/adr/0005-state-substrate-and-migrations.md)). The schema is versioned: an applied migration is immutable and a version gap is refused. | enforced |

## The six boundary confusions (Part 1)

Collapsing any of these pairs is the root cause of most agent failures. Every review
and every `/stack-audit` asks these six questions.

| Keep apart | Because |
|---|---|
| **Session** vs **authorization** | A session says which record to load. Authorization says what this run may do. Same session ≠ same permissions. |
| **Transcript** vs **context** | The transcript is the durable record. Context is the bounded payload assembled for one turn. The model sees a prepared view. |
| **Memory** vs **learning** | Memory is durable state re-injected later. It is not a weight update, and it is not the transcript. |
| **Capability** vs **execution** | A tool schema exposes what may be *requested*. The execution surface determines what actually *happens*. |
| **Approval** vs **isolation** | Approval decides whether to proceed. Isolation limits what the action can do once it does. You need both. |
| **Observability** vs **evaluation** | Observability produces evidence. Evaluation judges whether the evidence meets criteria. A perfect trace of bad behavior is still bad behavior. |

## Where each part of the series is enforced

| Part | Enforced by |
|---|---|
| 1 — systems view | This ledger; `.importlinter` contracts 1–4; `/stack-audit` |
| 2 — foundation | `SPEC.md` § Foundation Assumptions; ADR-0003 |
| 3 — control planes | `tests/fitness/test_session_ownership.py` |
| 4 — runtimes & durable execution | `test_run_identity.py`, `test_idempotency.py`, `test_waiting_is_state.py`; ADR-0002 |
| 5 — context & memory | `test_context_assembly.py`, `test_memory_is_explicit.py` |
| 6 — tools & MCP | `test_tool_registry.py`, `test_capability_is_not_execution.py` |
| 7 — execution & approvals | `test_approval_boundary.py`, `test_identity_envelope.py`, `test_untrusted_content.py` |
| 8 — observability & evaluation | `test_trace_completeness.py`, `test_audit_separate_from_traces.py`, `evals/` release gates |
