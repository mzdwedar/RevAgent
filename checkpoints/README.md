# Checkpoints

The shape of a checkpointed turn, as released, and what to do when it changes.

- `schema.json` is the shape this code writes: the keys of `TurnState` with their
  types, the graph's nodes, and `CHECKPOINT_SCHEMA_VERSION`. Generated, never
  hand-edited: `uv run python scripts/checkpoint_guard.py --write`.
- `vN.md` is the migration note for version N, required when N was an incompatible
  change. It is what the operator reads when `agentstack-operator stalled` lists runs
  parked in `needs_migration`.

`scripts/checkpoint_guard.py` compares the code against `schema.json` as it stands at
the last release (the base branch in CI). Adding a key or a node is compatible and
needs nothing. Removing or retyping a key, or removing or renaming a node, is not, and
the build fails until the change:

1. bumps `CHECKPOINT_SCHEMA_VERSION` in `src/agentstack/runtime/graph.py`;
2. drops the old version from `COMPATIBLE_SCHEMA_VERSIONS`, so its runs park instead
   of resuming into the new shape;
3. adds `checkpoints/vN.md`, saying how to rewrite a parked checkpoint into the new
   shape and release its `needs_migration` wait.

A migration note should name the old and new version, what changed, how to rewrite
the checkpoint (`graph.update_state(...)` on the parked thread), and that the wait is
released with a resume naming the checkpoint it was parked on.
