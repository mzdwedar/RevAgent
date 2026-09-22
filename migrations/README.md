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
step records and waits.
