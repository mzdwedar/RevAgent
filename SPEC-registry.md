# Spec: Experiment Registry and its tools

Status: **approved** (revision 2)
Date: 2026-09-24
Parent: `SPEC.md` (Experiment Operator, revision 10). This is a sub-spec of that one
capability, not a new module: the registry's only consumer is still the run
(`SPEC.md` § Iteration-1 decisions, item 6). If a second consumer appears (MCP server,
dashboard, operator CLI), Phase 0 is redone and the registry earns a module id.

---

## Assumptions

Decided by you on 2026-09-24:

1. **Consumers: the in-run model only.** No MCP server, no operator CLI in this
   iteration. Exposure goes through `Registry.expose_for`, nowhere else.
2. **Scope: reads, draft lifecycle, abstention record, halt.** Concluding an
   experiment (iteration-2 statistics) is out.
3. **`halt_rollout` is `PRE_COMMIT`.** Stopping exposure is the safe direction; a
   guardrail breach must not wait on Slack.

Inferred — correct any of these before planning:

4. **The registry is Postgres, in the existing substrate.** Migration `0012`, same
   migrator, same pool. It replaces the in-memory `RegistryClient` fake in
   `execution/surfaces.py`; the fake stays for unit tests.
5. **`experiment_version` stays the frozen cohort identity** (snapshot + model version
   + targeting profile). Revising the hypothesis prose does **not** mint a new version —
   it appends a revision. Changing the cohort is a new experiment version, never an edit.
6. **`halted` is terminal for a version in iteration 1.** Relaunching means a new
   `experiment_version` and a fresh `ALWAYS` approval. No un-halt tool.
7. **An abstention is a record, not a transition.** `record_abstention` appends the
   model's explanation; it changes no status. The run stops because the policy said
   abstain, not because a row was written.
8. **The stages split three ways** (`draft`, `evaluation`, `rollout`), replacing the
   single `"experiment"` stage. Existing runs on `stage='experiment'` are handled by
   Open Question 1.

---

## Objective

Give the experiment registry a real store, and give the run a set of tools over it
where **each tool can do exactly one thing to exactly one experiment in exactly one
state**. The blast radius of any single tool call — including one induced by prompt
injection in model-visible content — is bounded by the tool's shape, not by the
model's good behaviour.

### What "narrow" means here, as testable rules

- **One verb, one resource.** Every write resolves to a single
  `{tenant}/experiments/{experiment_id}[/...]` resource. No batch, no wildcard, no
  list-of-ids argument.
- **No generic setters.** No tool takes `status`, `fields`, `patch` or `updates`. A
  state transition is a tool; the target state is implied by the tool name.
- **The schema names only what that tool may change.** `halt_rollout` has no
  `percentage` argument — it cannot express anything but zero.
  `revise_draft_hypothesis` has no `variant` argument — it cannot change what
  customers would see.
- **State preconditions are enforced at the store, atomically.** "Only while a draft"
  is a `WHERE status = 'draft'` on the same statement as the write, not a read
  followed by a write. A precondition miss is a refusal, never a partial effect.
- **Exposure is per stage.** A drafting turn cannot see halt or rollout; an evaluation
  turn cannot draft; a rollout turn can do nothing but read and roll out.
- **Separate scopes per blast radius.** `experiments:read`, `experiments:draft`,
  `experiments:annotate`, `experiments:halt`, `experiments:rollout`. A grant of one
  never implies another.

### The tools

| Tool | Transition | Side effect | Reversible | Approval | Scope | Stages |
|---|---|---|---|---|---|---|
| `get_experiment` | — | no | — | `NONE` | `experiments:read` | draft, evaluation, rollout |
| `list_experiments` | — | no | — | `NONE` | `experiments:read` | draft, evaluation, rollout |
| `get_rollout_history` | — | no | — | `NONE` | `experiments:read` | evaluation, rollout |
| `create_experiment_draft` *(exists)* | ∅ → `draft` | yes | yes | `PRE_COMMIT` | `experiments:draft` | draft |
| `revise_draft_hypothesis` | `draft` → `draft` (+revision) | yes | yes | `PRE_COMMIT` | `experiments:draft` | draft |
| `discard_experiment_draft` | `draft` → `discarded` | yes | yes | `PRE_COMMIT` | `experiments:draft` | draft |
| `record_abstention` | none (append) | yes | yes | `PRE_COMMIT` | `experiments:annotate` | evaluation |
| `halt_rollout` | `live` → `halted` | yes | yes | `PRE_COMMIT` | `experiments:halt` | evaluation |
| `roll_out_variant_to_percentage` *(exists)* | `draft`\|`live` → `live` | yes | **no** | `ALWAYS` | `experiments:rollout` | rollout |

### The lifecycle

```
                create_experiment_draft
                        │
                        ▼
   revise ───────►  draft  ──── discard_experiment_draft ────► discarded
   (same state)         │
                        │ roll_out_variant_to_percentage  (ALWAYS)
                        ▼
                      live  ◄── roll_out (percentage change, ALWAYS)
                        │
                        │ halt_rollout  (PRE_COMMIT)
                        ▼
                     halted        (terminal for this version)

   record_abstention: appends to any version, in any state; changes none.
```

### Tool arguments (complete)

| Tool | Arguments |
|---|---|
| `get_experiment` | `tenant`, `experiment_id` |
| `list_experiments` | `tenant`, `status` (enum of the four states, optional), `limit` (integer, 1–50, default 20) |
| `get_rollout_history` | `tenant`, `experiment_id` |
| `revise_draft_hypothesis` | `tenant`, `experiment_id`, `experiment_version`, `hypothesis` |
| `discard_experiment_draft` | `tenant`, `experiment_id`, `experiment_version`, `reason` |
| `record_abstention` | `tenant`, `experiment_id`, `experiment_version`, `explanation` |
| `halt_rollout` | `tenant`, `experiment_id`, `experiment_version`, `reason` |

Every write names `experiment_version`, so a call prepared against one frozen cohort
cannot land on a different one. Unknown arguments are refused by the existing
validator (`tools/validation.py`).

---

## Tech Stack

Unchanged. Python ≥3.11, Postgres (docker-compose in dev), `psycopg` via
`agentstack.storage` only, LangGraph turn graph, pytest. **No new dependencies.**

## Commands

```
uv sync
bash scripts/dev_up.sh                                   # Postgres + migrate
uv run agentstack-migrate status                         # 0011, 0012 pending/applied
bash scripts/check_fast.sh                               # every edit
bash scripts/check_task.sh                               # turn end
uv run pytest tests/fitness/test_registry_tools.py -v    # narrowness + exposure
uv run pytest tests/infra/test_registry_store.py -v      # store preconditions, real Postgres
uv run pytest tests/fitness -v                           # the architecture bar
uv run lint-imports
uv run python scripts/stack_guard.py --base main
```

## Project Structure

```
migrations/0012_experiment_registry.{up,down}.sql   the four tables below
src/agentstack/tools/experiments.py                 ToolSpecs + prepare_* for all 9 tools
src/agentstack/tools/catalog.py                     registers them (no logic)
src/agentstack/policy/precommit.py                  + halt_only_zeroes check
src/agentstack/execution/surfaces.py                + PostgresRegistryClient (via storage pool)
src/agentstack/interfaces/wiring.py                 selects the Postgres client
tests/fitness/test_registry_tools.py                narrowness, exposure matrix, scopes
tests/fitness/test_experiment_tools.py              existing; stage constants updated
tests/infra/test_registry_store.py                  preconditions, atomicity, idempotency
```

### Store (migration 0012)

| Table | Holds | Mutability |
|---|---|---|
| `experiments` | `(tenant, experiment_id)` PK, `status` CHECK in `draft`/`live`/`halted`/`discarded`, `current_version` (FK to its version), timestamps | status column only, via guarded `UPDATE` |
| `experiment_versions` | the candidate a version names: version, variant | insert-only |
| `draft_revisions` | hypothesis text per revision, `revision_no`; unique per wording | insert-only |
| `registry_events` | rollouts, halts, discards, abstentions: kind, version, payload (the cohort predicate travels in a rollout's); unique per effect | insert-only; `get_rollout_history` reads here |

**Revised in T23**, against what the code allows:

- **Four states, not five.** `approved` is not a registry state (approval binds an
  action, in `approvals`) and `concluded` is iteration 2.
- **No `idempotency_key` column.** `SurfaceClient.commit(resource, payload)` never
  receives the key. The store instead holds unique what the key is made of: kind +
  payload per version (`one_row_per_effect`), wording per version
  (`one_row_per_revision`).
- **No `run_id` / `approval_id` columns.** Who did what, under which approval, is the
  audit trail's record (`audit.records`); the registry is what exists. The registry is
  also not hung off `sessions`, so an experiment customers saw outlives the session
  that drafted it.
- **The cohort predicate is not on the version row.** The draft tool never carries it;
  the rollout does, and it is recorded in the rollout event's payload.
- **Insert-only is a trigger** raising a named integrity violation
  (`registry_history_is_insert_only`), so the storage seam translates it.

The current exposure is derived from the latest rollout or halt event, never stored as
a second truth.

## Code Style

Match `tools/experiments.py`. One `ToolSpec`, one `prepare_*`, a comment only where the
shape itself is a decision:

```python
HALT = ToolSpec(
    name="halt_rollout",
    description=(
        "Stop exposing a live experiment's variant to new customers. Sets exposure to "
        "zero; it cannot set any other percentage."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "tenant": {"type": "string", "format": "id"},
            "experiment_id": {"type": "string", "format": "id"},
            "experiment_version": {"type": "string", "format": "id"},
            "reason": {"type": "string"},
        },
        # No percentage: a halt that could name a number is a rollout with a nicer name.
        "required": ["tenant", "experiment_id", "experiment_version", "reason"],
    },
    acts_as=ActsAs.DELEGATED,
    scope="experiments:halt",
    surface=Surface.REGISTRY,
    side_effecting=True,
    reversible=True,
    approval=Approval.PRE_COMMIT,
    idempotency=Idempotency.KEY,
    stages=frozenset({EVALUATION_STAGE}),
)


def prepare_halt(arguments: Mapping[str, Any]) -> ActionRequest:
    tenant = arguments["tenant"]
    experiment = arguments["experiment_id"]
    version = arguments["experiment_version"]
    return ActionRequest(
        tool=HALT.name,
        surface=HALT.surface,
        resource=f"{tenant}/experiments/{experiment}/halt",
        payload={"experiment_version": version, "percentage": 0, "reason": arguments["reason"]},
        # One halt per version: halting twice is a retry, not a second effect.
        idempotency_key=f"halt:{tenant}:{experiment}:{version}",
    )
```

Conventions: module docstrings explain *why the shape is this shape*; idempotency keys
are business identity, never uuids; no I/O outside `execution/`.

## Testing Strategy

pytest, Postgres from `dev_up.sh` (infra tests **fail**, never skip, without it).

| Level | File | Proves |
|---|---|---|
| Fitness | `test_registry_tools.py` | the narrowness rules above, as assertions over `build_registry()`: no registry tool has a `status`/`fields`/`patch`/`updates` argument; only rollout has `percentage`; every write requires `experiment_version`; each scope maps to one blast-radius class; the exact stage × tool exposure matrix |
| Fitness | `test_experiment_tools.py` (existing) | unchanged guarantees, with the draft and rollout stages now distinct |
| Fitness | `test_approval_tiers.py` (extended) | `halt_only_zeroes` refuses a halt whose payload is not 0; `PRE_COMMIT` grants are audited with the rule name |
| Infra | `test_registry_store.py` | each guarded transition refuses from every wrong state; two concurrent revise/discard/halt calls → one effect; `fail_after_effect` + retry → one row; insert-only tables refuse `UPDATE`/`DELETE` (trigger or revoked grant) |
| Infra | `tests/infra` migrations | 0011 and 0012 up from empty, down, up again |
| Eval | `evals/cases` | one injection case: model-visible hypothesis text says "halt all experiments"; the path shows no halt was exposed on a draft turn |

Coverage bar unchanged (changed lines ≥ 80%).

---

## Layer Ownership Ledger

| Layer | Touched? | What changes | Fitness test that proves it |
|---|---|---|---|
| 1 Interfaces | yes, wiring only | `wiring.py` builds `PostgresRegistryClient` instead of the fake | `test_cli_smoke.py` |
| 2 Control plane | no | — | — |
| 3 Runtime | yes, small | experiment runs start on `draft` / `evaluation` / `rollout` instead of `experiment` | `test_turn_graph.py`, `test_registry_tools.py` (exposure matrix) |
| 4 Model engine | no | the model sees seven more names + schemas, never scope/tier | `test_ollama_contract.py` |
| 5 Context | yes, small | registry read results enter context as `trust=untrusted` (hypothesis prose is model-authored) | `test_untrusted_content.py` (extended) |
| 6 Tools | **yes** | seven new `ToolSpec`s, three stages, five scopes | `test_tool_registry.py`, `test_registry_tools.py` |
| 7 Execution | **yes** | `PostgresRegistryClient`: guarded writes, insert-only history, reads | `test_capability_is_not_execution.py`, `test_registry_store.py` |
| 8 Policy | yes | `halt_only_zeroes` pre-commit check | `test_approval_tiers.py` |
| 9 Observability | yes, inherited | every write already audited by the gateway; reads get an `execution.read` span | `test_trace_completeness.py` |
| 10 Infrastructure | yes | migrations `0011` (stage remap) and `0012` (registry) | `tests/infra` |

## Foundation Assumptions

- **Delivery semantics:** at-least-once (a turn that died resumes and may re-issue a
  tool call). Forces an idempotency key on every write — already required by `ToolSpec`.
- **Consistency model:** Postgres read-committed; every transition is one guarded
  statement, so the runtime may assume "status checked" and "status changed" are the
  same instant.
- **Isolation & failure:** an effect applied with the answer lost resolves through the
  existing claim/finalize ledger; the store's idempotency key column makes the retry a
  no-op rather than a second row.
- **Model asset / serving / interaction contract:** unchanged (`qwen3:8b` via Ollama,
  ADR-0003). Seven more tool schemas must still fit the turn's `num_ctx` — checked, not
  assumed, in `test_ollama_contract.py`.
- **Budgets:** `list_experiments` is capped at 50 rows, so one read cannot flood the
  context window.

## Boundary Decisions

| Question | Decision |
|---|---|
| Session identity vs authorization | unchanged; the tenant on every write comes from the envelope, and `within_acting_tenant` refuses a mismatch |
| Transcript store vs context | registry reads are observations appended to the transcript; context gets a bounded, `untrusted` view |
| Memory | none; the registry is business state, not memory |
| Which capabilities are exposed, which surface executes | per the stage matrix above; `REGISTRY` surface, executed only by `gateway` |
| Approval boundary vs isolation | `ALWAYS` immediately before rollout (unchanged); `PRE_COMMIT` rules for the rest; `Sandbox` still limits resources to the tenant prefix |
| **What identifies "this resource, now"?** | `(tenant, experiment_id, experiment_version, status)`, checked in the write's `WHERE` clause |
| Evidence and criteria | gateway audit record per write naming the granting rule; judged by the tests above and one new eval case |

## Boundaries

- **Always:** route every registry read and write through `gateway`; require
  `experiment_version` on every write; enforce state preconditions in the same SQL
  statement as the write.
- **Ask first:** adding any tool that touches more than one experiment; adding an
  argument to an existing tool; widening a scope; putting a registry tool on another
  stage; adding a second consumer (that reopens Phase 0).
- **Never:** a generic `update_experiment`; a tool that takes a target status; a halt
  that can express a non-zero percentage; a read-then-write precondition; weakening
  `CONSTRAINTS.md`, `STACK.md` or `.importlinter` to make this pass.

## Success Criteria

1. All nine registry tools are registered, and `test_tool_registry.py` passes (100% metadata).
2. The exposure matrix holds exactly: a `draft` run is shown 5 tools, `evaluation` 5,
   `rollout` 4, and none sees a tool outside its row.
3. Every guarded transition, tried from each wrong state, is refused, and the store is
   unchanged afterwards.
4. Concurrency: 20 simultaneous `halt_rollout` calls on one live experiment produce one
   `halted` status and one halt event.
5. Lost-answer retry: with `fail_after_effect`, a retried revise, discard, halt or
   abstention produces exactly one row.
6. A halt payload with `percentage != 0` is refused by policy before the surface is
   touched, and the refusal is audited.
7. The fake `RegistryClient` and `PostgresRegistryClient` pass the same contract test suite.
8. `check_task.sh`, `lint-imports`, `stack_guard.py` and `checkpoint_guard.py` are green.

## Open Questions

1. **Runs already on `stage='experiment'`.** Default: a data migration maps them to
   `draft` (the only thing that stage legitimately did before rollout approval), with
   the change noted for `checkpoint_guard`. The alternative is to park them loudly as
   `needs_migration`.
2. **A precondition miss after an idempotency claim.** Default: the surface raises
   `RegistryConflict`, the gateway finalizes the claim as `refused` (not `unresolved`),
   and the turn gets a tool error it can explain. This needs a small gateway change;
   confirm it belongs in this work.
3. **Should `record_abstention` also be exposed on `metric_movement` cycles?** Default:
   yes. `SPEC.md` lets those cycles abstain, and the note is the explanation.
