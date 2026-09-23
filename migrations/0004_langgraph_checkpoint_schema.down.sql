-- CASCADE because the tables inside were not created by this migration and are not
-- named here; dropping the schema is the only honest way to undo creating it.
DROP SCHEMA langgraph CASCADE;
