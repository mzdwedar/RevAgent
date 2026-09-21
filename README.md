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

## See it run

```bash
uv sync
uv run agentstack
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
| `src/agentstack/` | nine layers, dependency direction enforced by `.importlinter` |
| `tests/fitness/` | 22 tests, one per collapsed-boundary failure mode |
| `evals/` | release gates that judge the path, not just the answer |
| `scripts/` | the three check stages and the bar guard |
| `.claude/` | hooks, the `agent-stack-auditor` subagent, and `/spec` `/plan` `/stack-audit` |

## Workflow

`/spec` → `/plan` → `/build` → `/review` → `/stack-audit` → `/ship`.

`/spec` and `/plan` are extended locally: a spec must carry the Layer Ownership
Ledger, Foundation Assumptions and Boundary Decisions; a task must name its layer and
the test that proves it. `/stack-audit` is mandatory before `/ship` and covers the
architectural judgement a test cannot make — tool granularity, approval legibility,
whether a memory should exist at all.

## What is deliberately unfinished

The in-memory stores in `control_plane`, `runtime` and `context` are reference
implementations. The durable-execution backend is an open decision
(`docs/adr/0002`); the fitness tests assert the invariants rather than a vendor, so
whichever backend wins has to satisfy them rather than replace them.
