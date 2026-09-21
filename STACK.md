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
| 1 | Interfaces & channels | `agentstack.interfaces` | — | Entry points carry an event; they never resolve identity, policy or state themselves. Nothing imports this layer. | enforced |
| 2 | Control plane & session ownership | `agentstack.control_plane` | `TranscriptStore`, `WorkingStateStore` | `session_id` is never the user id. Transcript, working state and memory are three distinct stores. The runtime receives a bounded `SessionView`, never the raw record. | enforced |
| 3 | Runtime, workflows, durable execution | `agentstack.runtime` | step ledger + wait store (backend: ADR-0002) | Every run has a stable `run_id`. Side effects happen only inside a recorded step. Waiting is persisted state, not a sleeping process. Replay rebuilds state; it never re-rolls non-determinism. | enforced |
| 4 | Model engine & inference | `agentstack.model` | — | Model asset, serving system and interaction contract are three separate choices, recorded separately (ADR-0003). Tool calls that come back are **proposals**, not executions. Compatible is not equivalent. | enforced |
| 5 | Context, retrieval, memory | `agentstack.context` | retrieval index + `MemoryStore` | Prompt context is a derived, inspectable view — never the canonical record. Every item carries scope, provenance, freshness, reason and trust. Memory writes are explicit. Maintenance runs off the hot path. Retrieval returns candidates, not authority. | enforced |
| 6 | Tools, MCP, capability surfaces | `agentstack.tools` | tool registry | A schema validates shape; it never grants authority. Tools are narrow and intent-specific. Exposure is filtered per run. Tool output is an untrusted observation. | enforced |
| 7 | Execution surfaces | `agentstack.execution` | idempotency ledger | Every action names its surface. `gateway.execute` is the only path to a real client. Every side effect carries an identity envelope and an idempotency key. | enforced |
| 8 | Identity, trust, policy, approvals | `agentstack.policy` | approval store (audit sink) | Approval sits immediately before the irreversible act and binds run id, action fingerprint, state snapshot and approver. Untrusted content never increases available authority. | enforced |
| 9 | Observability, evaluation, feedback | `agentstack.observability` | trace sink **and a separate** audit sink | Traces cross the whole stack, not just the model call. Audit records are not debug logs. Evaluation judges the path, not only the answer. A failure is not closed until it is a regression case. | enforced |
| 10 | Infrastructure substrate | — | — | Inherited, not written. Delivery, consistency, isolation and failure semantics are recorded as assumptions in `SPEC.md` (see the Foundation Assumptions section) and must be stated before the runtime depends on them. | documented |

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
