-- Part 7: three approval tiers, three behaviours.
--
-- `PRE_COMMIT` and `ALWAYS` were declared as different tiers and implemented
-- identically: both demanded a human-granted record. A tier nothing enforces is a
-- comment, and this one reads as a promise that some actions proceed on policy alone.
--
-- Giving `PRE_COMMIT` real semantics means approvals now come from two sources, and the
-- difference has to be on the record. Without it, a policy grant and a human decision
-- are the same row, and "who approved this refund" has no answer.

ALTER TABLE approvals ADD COLUMN granted_by text NOT NULL DEFAULT 'human';
ALTER TABLE approvals ADD COLUMN rule text;

ALTER TABLE approvals
    ADD CONSTRAINT approval_grant_kind_is_known CHECK (granted_by IN ('human', 'policy'));

-- A policy grant names the rule that made it, and a human grant does not have one.
-- A policy approval that cannot say which rule permitted it is unauditable, and a
-- human approval carrying a rule name is a policy grant wearing a person's name.
ALTER TABLE approvals
    ADD CONSTRAINT grants_name_their_source CHECK ((granted_by = 'policy') = (rule IS NOT NULL));

COMMENT ON COLUMN approvals.granted_by IS
    'human = a person was shown the summary and said yes. policy = a PRE_COMMIT rule '
    'permitted it and nobody was woken. An ALWAYS action is never satisfied by policy.';
