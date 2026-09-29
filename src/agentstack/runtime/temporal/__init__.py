"""The run's orchestration on Temporal (SPEC-durable-runtime.md, ADR-0007).

Temporal owns *where* a run is. Postgres keeps owning *what happened*: `runs`, `waits`,
`run_steps`, `approvals`, `idempotency_claims` and `audit` are the record, exactly as
before. Nothing here is imported for its side effects, and this file imports nothing:
the workflow sandbox re-imports whatever a workflow module pulls in, and a package
`__init__` is part of that.
"""
