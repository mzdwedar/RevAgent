"""Layer 3 - runtime, workflows, durable execution.

A loop can answer a turn. A durable workflow can survive time. This package owns
progress - run identity, step boundaries, persisted waits, resume - not authority.
The backend is Temporal (SPEC-durable-runtime.md, ADR-0007); the invariants are not
negotiable whichever backend runs them.

This file imports nothing. It used to re-export the runtime's main names, which meant
importing *any* submodule loaded all of them - the gateway and the database driver
included. The Temporal workflow sandbox re-imports a workflow's parent packages, so
that re-export put a path to an effect inside workflow code at runtime, whatever
contract 6 said at lint time. Import from the submodule that defines the name.
"""
