DROP INDEX audit.audit_records_by_wait;
ALTER TABLE audit.records DROP COLUMN state_snapshot;
ALTER TABLE audit.records DROP COLUMN wait_id;
ALTER TABLE waits DROP CONSTRAINT recorded_actions_are_whole;
ALTER TABLE waits DROP COLUMN action_arguments;
ALTER TABLE waits DROP COLUMN action_tool;
