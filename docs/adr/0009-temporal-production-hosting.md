# ADR-0009: Where Temporal runs in production

- Status: **deferred, with the invariants fixed** (the shape of ADR-0002)
- Date: 2026-09-24
- Layer: 10 (infrastructure substrate), serving 3
- Raised by: `SPEC-durable-runtime.md`

## Context

`SPEC-durable-runtime.md` adopts Temporal as the run orchestrator. In dev, Temporal runs as a
`docker-compose.yml` service. Nothing is deployed yet, and choosing production hosting now
would mean choosing before the first deploy tells us what it needs.

## Options

| Option | Gives us | Costs |
|---|---|---|
| **Temporal Cloud** | Managed history store, upgrades, multi-region option, SLA | Per-action pricing. Run payloads leave our infrastructure, so the payload codec (encryption) becomes mandatory, not optional. |
| **Self-hosted, Postgres persistence** | One database technology, data stays in-house | We operate the Temporal server: its schema migrations, upgrades and history shard count, which can't change after creation |
| **Self-hosted, Cassandra/other** | Scale beyond Postgres | A second database technology to operate. Not justified at a target of hundreds of runs. |

## Decision

Deferred. Whatever is chosen must keep the following true, and the spec's tests assert them
against the dev server:

1. **The dedupe window is not Temporal's retention.** Workflow-id reuse policies only hold
   while a closed workflow is retained. Trigger dedupe stays on the Postgres cycle claim
   (`runtime/cycles.py`), so no retention setting can reopen a settled trigger.
2. **Nothing secret or bulky enters history.** No identity envelope, credential, prompt or
   evidence bundle. Only ids and small verdicts. This holds whether or not a payload codec
   exists, so the choice of host can't weaken it.
3. **The server being down freezes runs; it never loses them.** Workers retry their
   connection, and no effect happens outside an activity.
4. **Temporal's history is not the audit.** `audit.records` in our Postgres stays the
   accountability record, whatever the host keeps and for however long.

## What would decide it

- The first real deploy target and its data-residency rules. If run payloads can't leave our
  infrastructure, that decides it (self-hosted, or Cloud with a codec we hold the keys for).
- Measured action volume at the verified concurrency (100 runs) turned into Cloud cost,
  compared with the operating cost of a self-hosted cluster.
- Whether the team will own a stateful service's upgrades (shard count is fixed at creation).
