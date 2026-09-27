# Migrations

Versioned SQL, applied by `agentstack.storage.migrate`. One transaction per file.

```
NNNN_short_name.up.sql      required
NNNN_short_name.down.sql    required unless the step is genuinely one-way
```

Rules the migrator enforces, so they are not conventions anyone has to remember:

- **Versions are contiguous from 0001.** A gap means a migration was lost, not skipped.
- **An applied migration is immutable.** Its checksum is recorded; editing it afterwards
  is refused rather than silently re-run, because two databases would then disagree
  about what `0001` means. Fix forward with a new version.
- **A file without a `.down.sql` cannot be rolled back** — the rollback refuses rather
  than stopping halfway through.

`0001` is the control plane (T2): sessions, transcript events and working state —
three tables, because they are three stores. `0002` is the runtime (T3): runs,
step records and waits. `0003` is approvals, the idempotency ledger and the audit
trail — the last in a schema of its own. `0004` creates the `langgraph` schema, whose
tables are created and versioned by `PostgresSaver.setup()` rather than from here.
`0005` is the trigger cycle ledger: one evaluation per (experiment, watermark, kind).
`0006` records whether an approval came from a person or from a rule, and which rule.
`0007` remembers which Slack deliveries were accepted, so a replay is refused.
`0008` is the approver group, per tenant, with who added each member.
`0009` lets an approval wait record what it is asking about, for the process that answers.
`0010` gives every pending trigger and approval wait a deadline, and counts re-asks.
`0011` moves runs off the retired `experiment` stage onto `draft`, one of the three that replaced it.
`0012` is the experiment registry: experiments, their versions, draft revisions and events — the last three insert-only.
`0013` records which run is an experiment's, so a trigger can find (or start) the one Temporal workflow for it.
`0014` records the frozen cohort a proposal rests on: its predicate for the rollout, its size and value for the approver. Insert-only.
