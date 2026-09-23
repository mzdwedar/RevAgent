-- A schema of LangGraph's own, for tables LangGraph creates and versions itself.
--
-- `PostgresSaver.setup()` issues its own DDL and tracks it in its own ledger table,
-- `checkpoint_migrations`. That is correct - the checkpoint layout belongs to the
-- library, and copying its DDL into `migrations/` would couple this repository to a
-- vendor's internals and give one set of tables two version histories that could
-- disagree.
--
-- What we own is the boundary. This migration creates the schema; the saver fills it,
-- through a pool whose search_path points here and nowhere else. The effect is that
-- `\dt` in `public` shows this system's tables and only this system's tables, and that
-- rolling our schema back to zero cannot leave four unexplained tables behind.

CREATE SCHEMA langgraph;

COMMENT ON SCHEMA langgraph IS
    'Owned by langgraph-checkpoint-postgres. Tables here are created and migrated by '
    'PostgresSaver.setup(), not by agentstack migrations. See docs/adr/0006.';
