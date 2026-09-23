-- A wait has to identify the run, the pending wait, the state it was paused against,
-- and the input that satisfies it. For a human approval it also has to identify **what
-- is being approved**, and it did not.
--
-- That gap only shows up across a process death, which is criterion 23. The process
-- that parked the wait holds the prepared `ActionRequest` and the rendered prompt in
-- memory; the process that handles the human's answer an hour later holds neither. It
-- cannot bind an approval to a fingerprint it never computed, and it cannot record
-- what the approver was shown.

ALTER TABLE waits ADD COLUMN action_fingerprint text;
ALTER TABLE waits ADD COLUMN approval_summary text;

-- Only approval waits need these: a trigger wait is not about a specific action.
-- Written as an implication rather than NOT NULL so the column stays honest for the
-- other kinds instead of being filled with a placeholder.
ALTER TABLE waits ADD CONSTRAINT approval_waits_name_their_action CHECK (
    kind <> 'human_approval'
    OR (action_fingerprint IS NOT NULL AND btrim(approval_summary) <> '')
);
