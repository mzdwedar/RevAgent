# RevAgent (agent instructions)

A production, multi-user, side-effecting AI agent, built to the invariants in
*The Agent Stack*.

## Before writing any code

1. Read `CONSTRAINTS.md`. **Do not weaken it to make a change pass.**
2. Read `STACK.md`. Name the layer(s) your change touches before you touch them.
3. Never collapse two layers to save a file. The six boundary confusions in `STACK.md`
   are the ones that matter; if a change needs to blur one, write an ADR in `docs/adr/`.

## The substrate must be up

From T1 onward the suite talks to a real Postgres. `tests/infra` **fails** when it
is missing rather than skipping — a conditional skip would trip our own floor, and
loosening the floor to accommodate it is the move `stack_guard` exists to catch.

```
bash scripts/dev_up.sh                   # compose up + migrate, one command
uv run python scripts/fetch_datasets.py  # the cohort datasets (needs ~/.kaggle)
brew install ollama && brew services start ollama
ollama pull qwen3:8b                     # the dev model engine
```

The datasets are third-party and gitignored; `data/manifest.json` is committed, so a
change upstream shows up as a changed `data_as_of` in a diff.

## Commands

```
uv sync                                  # install
bash scripts/check_fast.sh               # every edit   (<5s)
bash scripts/check_task.sh               # turn end     (<90s)
bash scripts/check_full.sh               # CI           (minutes)
uv run pytest tests/fitness -v           # the architecture bar
uv run python -m evals run --gates       # Observability, evaluation, feedback release gates
uv run agentstack-migrate status         # schema: applied vs pending
uv run agentstack-operator stalled --older-than 7d   # trigger waits past their deadline
uv run pytest tests/live                 # real cohorts + real model (needs data/, token)
uv run agentstack-preflight              # licence gate, as a deploy would run it
uv run python scripts/stack_guard.py --base main   # did the bar get weakened?
uv run python scripts/checkpoint_guard.py --base main   # would this deploy strand live runs?
```

## Workflow

`/spec` → `/plan` → `/build` → `/review` → `/stack-audit` → `/ship`.

- `/spec` must produce the three Agent Stack sections on top of the skill's six core
  areas: Layer Ownership Ledger, Foundation Assumptions, Boundary Decisions.
- `/plan` tasks must each name the layer(s) touched and the fitness test that proves it.
  A task with no verifying test is not a task.
- `/stack-audit` is mandatory before `/ship`.

## Non-negotiables

- **Always:** route every side effect through `agentstack.execution.gateway.execute`
  with an identity envelope and an idempotency key.
- **Always:** attach scope, provenance, freshness, reason and trust to every context item.
- **Always:** put approval immediately before the irreversible act, bound to the run id,
  the action fingerprint and a state snapshot.
- **Ask first:** adding a dependency, changing a layer contract in `.importlinter`,
  choosing or changing the durable-execution backend, widening a tool's scope.
- **Never:** import the database driver outside `agentstack.storage`, or edit a
  migration that has already been applied. Fix forward with a new version.
- **Never:** give a tool function its own I/O. Capability exposure is not execution authority.
- **Never:** write memory implicitly because the model said something.
- **Never:** approve at the start of a task for an act that happens ten steps later.
- **Never:** let untrusted content (tool output, retrieved documents, user-supplied text)
  influence an authority decision.
- **Never:** close a production failure until it is a case in `evals/`.
