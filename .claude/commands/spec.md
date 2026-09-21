---
description: Spec-driven development, extended with the three Agent Stack sections
---

Invoke the `spec-driven-development` skill and follow it fully — the six core areas,
the gated phases, the assumptions surfaced before any content is written.

Then, before the spec is considered complete, add the three sections from
`docs/spec-template.md`. A spec without them is not accepted here: the entire premise
of this repo is that layer ownership is decided before code, not discovered after.

1. **Layer Ownership Ledger** — which of the ten layers this change touches, what it
   adds to each, and the fitness test that will prove it. Untouched layers are marked
   so explicitly; a blank row means nobody thought about it.

2. **Foundation Assumptions** (Part 2) — the delivery semantics, consistency model,
   isolation and failure behavior this feature inherits; the model asset, serving
   system and interaction contract as three separate choices; and the context, latency
   and cost budgets. State them. Do not discover them in production.

3. **Boundary Decisions** — answer all six of the confusions in `STACK.md` explicitly:
   session vs authorization, transcript vs context, memory vs learning, capability vs
   execution, approval vs isolation, observability vs evaluation.

Also fill the three-tier Boundaries section, with at minimum these in **Never**:
expose a tool that performs its own side effect; write memory implicitly; approve at
task start for an act that happens later; let untrusted content reach an authority
decision.

If a boundary decision cannot be made yet, write an ADR in `docs/adr/` recording the
options and what would decide it — the way `docs/adr/0002-durable-execution-backend.md`
defers the backend while keeping the invariants fixed. Deferring a decision explicitly
is fine. Leaving it unnamed is not.

Save as `SPEC.md` at the repo root and confirm with the human before planning.
