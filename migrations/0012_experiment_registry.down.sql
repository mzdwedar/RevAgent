ALTER TABLE experiments DROP CONSTRAINT current_version_exists;
DROP TABLE registry_events;
DROP TABLE draft_revisions;
DROP TABLE experiment_versions;
DROP TABLE experiments;
DROP FUNCTION registry_history_is_insert_only();
