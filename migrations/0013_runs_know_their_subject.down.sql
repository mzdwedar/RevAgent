-- Lossy by necessity: the old code has nowhere to keep a subject, so evaluation runs go
-- back to tenant-wide authority. That is the state H3 describes, restored on purpose.
ALTER TABLE runs DROP CONSTRAINT evaluation_runs_name_their_subject;
ALTER TABLE runs DROP CONSTRAINT run_subject_is_an_identifier;
ALTER TABLE runs DROP COLUMN subject;
