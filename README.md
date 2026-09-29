# revenuecat-agent

A production, multi-user, side-effecting AI agent, built to the invariants in
[The Agent Stack](https://theagentstack.substack.com/) (Parts 1–8), with those
invariants enforced by tests and hooks rather than by good intentions.

## The idea

The series argues that "agent" is too coarse a unit to debug, and that almost every
production failure is one of six collapsed boundaries: session/authorization,
transcript/context, memory/learning, capability/execution, approval/isolation,
observability/evaluation.

Prose describing those boundaries survives until the next agent is in a hurry. So here
each one is a layer with an owner (`STACK.md`), a number in the bar (`CONSTRAINTS.md`),
and a test that fails when the boundary is collapsed (`tests/fitness/`).

## Licensing, before anything else

**The churn model at the centre of this system is licensed for non-commercial use.**

This repository declares no licence of its own yet. What it does depend on is settled:
`agentstack.prediction` scores churn with PriorLabs' **TabPFN-3.5**, whose weights are
open for **non-commercial use only**. Using them means accepting that licence once per
machine, through a gated Hugging Face repository:

1. Register at <https://ux.priorlabs.ai> and accept the licence on the **Licenses** tab
2. Copy the API key from <https://ux.priorlabs.ai/account>
3. `export TABPFN_TOKEN="<your-api-key>"`

**What this means in practice.** Everything here is shaped like a production system —
durable runtime, multi-tenant, side-effecting, approval-gated — and the model at the
centre of it cannot be shipped in a commercial product under this licence. That is a
real constraint, not a setup step, and it is stated here rather than discovered during
a launch review.

If you need a commercial path, the checkpoint is one constant:
`CHECKPOINT` in `src/agentstack/prediction/engine.py`. `tabpfn` 9.x gates v2.5, v2.6,
v3, v3.5 and v3.5-fast; **v2 is ungated** and needs no acceptance step at all. Whether
v2's own terms suit your use is a question for its licence — this note records only
that it does not require the gate above.

The checkpoint is named explicitly rather than inherited from the package default,
because the model version travels in every experiment's `experiment_version` and a
silent upgrade would make recorded provenance wrong.

## See it run

```bash
uv sync
bash scripts/dev_up.sh        # Postgres via docker compose, then migrations
uv run agentstack
```

The walkthrough above needs only Postgres. The full workflow also wants a model engine
and the cohort data:

```bash
brew install ollama && brew services start ollama
ollama pull qwen3:8b                      # dev only; production runs on a cloud GPU
uv run python scripts/fetch_datasets.py   # needs ~/.kaggle/kaggle.json
```

One refund walks the whole stack: context assembled and fingerprinted, tools exposed
for this run only, a policy decision, a run parked on a human approval bound to that
exact action, a resume against the same run id, the commit — and then two retries that
are deduplicated instead of refunding three times.

## The gates

| Command | What it checks | Budget | When |
|---|---|---|---|
| `bash scripts/check_fast.sh` | lint, format, types, layer contracts, capability metadata | < 5s | every edit (PostToolUse hook) |
| `bash scripts/check_task.sh` | the above + full suite + changed-line coverage + bar integrity | < 90s | turn end (Stop hook) |
| `bash scripts/check_full.sh` | the above + release gates + secrets + dependencies | minutes | CI |
| `uv run python scripts/stack_guard.py --base main` | did the bar itself get weakened? | seconds | task end, CI |
| `uv run python -m evals run --gates` | the Part-8 release gates | seconds | CI |

The hooks in `.claude/settings.json` run the first two automatically and block on
failure, so the loop fails closed whether or not anyone remembers to run them.

## Layout

| Path | Purpose |
|---|---|
| `STACK.md` | the layer ledger: owner module, store, invariants, status |
| `CONSTRAINTS.md` | the bar, with numbers and the command that produces each verdict |
| `CLAUDE.md` / `AGENTS.md` | what an agent must read before writing code here |
| `docs/spec-template.md` | six core areas plus the three Agent Stack sections |
| `docs/adr/` | one ADR per boundary decision |
| `src/agentstack/` | 11 packages across the ten layers, dependency direction enforced by six `.importlinter` contracts |
| `migrations/` | versioned SQL; an applied migration is immutable, a version gap is refused |
| `experiments/` | targeting thresholds, versioned — change a number here, not in code |
| `tests/fitness/` | 44 tests, one per collapsed-boundary failure mode |
| `tests/live/` | checks needing real datasets or the model; excluded from CI, declared in `CONSTRAINTS.md` |
| `tests/durability/` | spawns real worker processes, kills them with SIGKILL, and a fresh one resumes the run from Temporal's history and the Postgres record |
| `evals/` | 13 release gates that judge the path, not just the answer |
| `scripts/` | the three check stages, the bar guard, and the checkpoint and replay guards |
| `.claude/` | hooks, the `agent-stack-auditor` subagent, and `/spec` `/plan` `/stack-audit` |

## Workflow

`/spec` → `/plan` → `/build` → `/review` → `/stack-audit` → `/ship`.

`/spec` and `/plan` are extended locally: a spec must carry the Layer Ownership
Ledger, Foundation Assumptions and Boundary Decisions; a task must name its layer and
the test that proves it. `/stack-audit` is mandatory before `/ship` and covers the
architectural judgement a test cannot make — tool granularity, approval legibility,
whether a memory should exist at all.

## What is deliberately unfinished

- **`context.MemoryStore` and `MaintenanceQueue` are still process-local.** Layers 2,
  3, 7, 8 and 9 are on Postgres; layer 5's memory never got a durability task. Found by
  auditing the stores at Checkpoint A rather than by trusting the task list, and
  recorded in `tasks/todo.md` instead of quietly left.
- **No recorded TabPFN scores yet** (`data/scores/`). Producing them needs the licence
  above. Everything around them — the gate, the fold assignment, the cross-fitting, the
  replay guards — is built and tested without it.
- **Two runtimes, one record.** A turn is a checkpointed LangGraph graph
  (`docs/adr/0006`); a run is a Temporal workflow (`docs/adr/0007`–`0009`). Both own
  position only: waits, claims, approvals and audit stay in Postgres, and the fitness
  tests assert those invariants rather than either vendor. Still open: no reconcile
  command for an unresolved effect, and `operator stalled` does not list reconcile waits
  (`operator status` shows them).
- **Credential storage and PII retention** are named debts, not oversights.
  `DATABASE_URL` and `TABPFN_TOKEN` are environment variables today.
