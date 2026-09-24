DROP INDEX waits_pending_by_deadline;
ALTER TABLE waits DROP CONSTRAINT pending_waits_have_a_deadline;
ALTER TABLE waits DROP CONSTRAINT reasks_count_up;
ALTER TABLE waits DROP COLUMN reasks;
ALTER TABLE waits DROP COLUMN deadline;
