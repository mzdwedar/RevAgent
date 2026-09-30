UPDATE run_steps SET status = 'failed' WHERE status = 'awaiting_approval';
ALTER TABLE run_steps DROP CONSTRAINT step_status_is_known;
ALTER TABLE run_steps ADD CONSTRAINT step_status_is_known
    CHECK (status IN ('started', 'completed', 'failed'));
