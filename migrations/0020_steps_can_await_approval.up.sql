-- A step that stops because a person has not yet said yes is not a failed step.
--
-- `step()` wrote `failed` for every exception, including the ApprovalRequired /
-- ApprovalStale that is the designed pause before an irreversible act. The ledger then
-- reported a failure that had not happened, and gave an operator nothing to tell it
-- apart from a real one. This adds the status that says what happened; the CHECK from
-- 0002 is replaced rather than edited, as an applied migration is never edited.
ALTER TABLE run_steps DROP CONSTRAINT step_status_is_known;
ALTER TABLE run_steps ADD CONSTRAINT step_status_is_known
    CHECK (status IN ('started', 'completed', 'failed', 'awaiting_approval'));
