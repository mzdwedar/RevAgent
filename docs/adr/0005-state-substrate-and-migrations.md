# ADR-0005: The state substrate is `agentstack.storage`, not `agentstack.execution`

- Status: accepted
- Date: 2026-09-22
- Layer: 10 (and the floor rule in `CONSTRAINTS.md` that names it)
- Supersedes part of: the floor rule amended on the same date

## Context

Every store in the repo was in memory. T2–T4 move sessions and transcripts, the step
ledger and wait store, and approvals, the idempotency ledger and audit records onto
Postgres. All of them need the driver.

The floor said: *"No module outside `agentstack.execution` imports an HTTP client, DB
driver, shell, socket or mailer."* Read literally, every one of those stores would have
to live in or go through layer 7.

## The problem with reading it literally

Layer 7 exists for systems the agent acts **upon**. Its gateway is the choke point where
an action meets a policy decision, an approval bound to a fingerprint and state
snapshot, an identity envelope, and containment. That machinery is the right answer for
issuing a refund or rolling out an experiment variant.

It is the wrong answer for the agent's own state:

- Recording that step 4 of 7 completed is not an effect anyone approves. Routing it
  through the approval path would mean either approving it (absurd) or adding a bypass
  through the one module whose value is having no bypass.
- Contract 2 forbids `agentstack.context`, `agentstack.tools` and `agentstack.model`
  from importing `agentstack.execution` at all. Under the literal reading,
  `context.memory` could never be persisted — the context layer's memory store would have to stay
  in RAM to satisfy an execution-surfaces rule.
- "Surface" would mean two different things, which is precisely the boundary collapse
  the whole ledger exists to prevent.

## Decision

A new bottom layer, `agentstack.storage`, holds the connection pool, the migration
runner, and later the concrete store implementations. It is the only package that
imports `psycopg`.

The floor rule is split in two — network/shell/mailer stay with `execution`, the
database driver goes to `storage` — and the amendment is recorded in
`CONSTRAINTS.md` under *Amendments to the floor*.

**Paired with a tightening.** The driver half of the rule was already enforced, but by
only one mechanism: `tests/fitness/test_layer_boundaries.py` scans the AST for a
`psycopg` import. `.importlinter` — the verdict `CONSTRAINTS.md` names for layer
boundaries — forbids `httpx, requests, urllib, subprocess, socket, sqlite3, smtplib`
and never mentioned `psycopg` at all. Contract 5 now does, so the rule holds under the
tool that reads the real import graph rather than one file's syntax tree.

The same fitness test previously exempted `execution` from *every* client. It now names
which layer may hold which: `execution` the lot, `storage` the two driver packages and
nothing more. A blanket storage exemption would have moved egress one package down
rather than containing it.

Net: the exempt set widened from one package to two, and both the widened rule and the
older one are checked by two independent mechanisms instead of one.

Contract 5 is separate from contract 3 and allows indirect imports, because every layer
reaches `storage` transitively by design — that is what a substrate is. The invariant is
about which module holds the driver handle, which is a direct import.

`stack_guard` could not see any of this: it watched the numbered tables for removed rows
and the Floor section for nothing at all. T1 is the first change to edit a floor bullet,
so T1 closed that blind spot in the same commit, with the *Amendments to the floor* log
as the way a deliberate change is acknowledged.

## Migrations: numbered SQL, not Alembic

Alembic is the de facto Python migration tool, and the project rule is to use the de
facto tool rather than hand-roll. It is still the wrong choice here.

Alembic's distinguishing feature is autogenerating migrations by diffing SQLAlchemy
models against the database. This repo has no ORM: the stores are frozen dataclasses and
hand-written SQL, deliberately. Adopting Alembic would mean adding SQLAlchemy — a large
dependency — to get `op.execute("...")` wrappers around SQL we would write by hand
anyway, plus a revision graph expressed as Python `down_revision` pointers instead of
filenames that sort.

What we actually need is versioning, ordering, a rollback path and an immutable history.
That is `migrations/NNNN_name.{up,down}.sql` plus a runner, and the runner is small
enough to read in one sitting. Three properties it enforces, each a way a deploy strands
a database:

- **Contiguous versions** — a gap means a migration was lost in a merge.
- **Immutable history** — the checksum of each applied migration is recorded. Editing one
  afterwards is refused, because after that two databases disagree about what `0001`
  means and nothing in the schema says which one you are looking at. Same failure class
  as T19's incompatible checkpoint: noticed, never guessed. Fix forward.
- **One migrator at a time** — a session-scoped advisory lock. Without it, two deploys
  racing both run `0001` and the loser fails halfway through its own transaction.

Revisit if an ORM ever arrives. Until then the seam is `migrations/`, and moving to
Alembic would be a mechanical translation of that directory.

## Synchronous, for now

The pool is `psycopg_pool.ConnectionPool`, not the async variant, because every existing
store, the gateway and the turn loop are synchronous. Mixing would mean an async seam
under a sync runtime — the worst of both. T8a adopts LangGraph and T21 sets a concurrency
target; if either forces async, it is one module's change, which is the point of the seam.

## Consequences

- **Postgres must be running to run the test suite.** `tests/infra` fails with the
  `scripts/dev_up.sh` command rather than skipping — a conditional skip would trip our
  own floor, and loosening the floor to accommodate it is the move `stack_guard` exists
  to catch. CI gets a service container.
- `agentstack.storage` is inside contract 1, so nothing can import *upward* out of it.
- T2–T4 add their tables as `0001`–`000n`. `migrations/` ships empty at T1: the runner is
  this task's deliverable, the schema is not.
- Credential storage remains the named iteration-2 debt. `DATABASE_URL` is an environment
  variable and the compose password is `agent`.
