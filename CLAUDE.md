# revenuecat-agent

A production, multi-user, side-effecting AI agent, built to the invariants in
*The Agent Stack* (Parts 1–8).

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
```

## Commands

```
uv sync                                  # install
bash scripts/check_fast.sh               # every edit   (<5s)
bash scripts/check_task.sh               # turn end     (<90s)
bash scripts/check_full.sh               # CI           (minutes)
uv run pytest tests/fitness -v           # the architecture bar
uv run python -m evals run --gates       # Part-8 release gates
uv run agentstack-migrate status         # schema: applied vs pending
uv run python scripts/stack_guard.py --base main   # did the bar get weakened?
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
