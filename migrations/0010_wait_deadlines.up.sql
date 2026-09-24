-- Criterion 4: a stalled run surfaces. And "approval timeout: re-ask, never silent expiry".
--
-- A run that is waiting looks exactly like a run that is stuck unless something asks,
-- and nothing can ask "is this overdue?" of a wait that never said when it would be.
-- `deadline` is one meaning for every kind: the time by which this wait should have
-- been satisfied. What happens past it depends on the kind - a trigger wait is
-- *stalled* and reported; an approval wait is *re-asked* and its deadline moves on -
-- but neither is ever allowed to simply lapse.

ALTER TABLE waits ADD COLUMN deadline timestamptz;

-- How many times the question has been put again. The first ask is not a re-ask.
ALTER TABLE waits ADD COLUMN reasks integer NOT NULL DEFAULT 0;
ALTER TABLE waits ADD CONSTRAINT reasks_count_up CHECK (reasks >= 0);

-- A pending wait that was parked before deadlines existed has been waiting unwatched
-- for however long it has been there. Making it due now is the honest backfill: a
-- trigger wait shows up as stalled, an approval is asked again. Inventing a deadline in
-- the future would hide exactly the waits this migration is for.
UPDATE waits SET deadline = now()
 WHERE NOT satisfied AND kind IN ('trigger', 'human_approval');

-- Only a *pending* wait has to carry one: a satisfied wait is not overdue for anything,
-- and requiring the column on history would mean inventing values for it.
ALTER TABLE waits ADD CONSTRAINT pending_waits_have_a_deadline CHECK (
    satisfied OR kind NOT IN ('trigger', 'human_approval') OR deadline IS NOT NULL
);

-- What `operator stalled` and the re-ask timer both ask: pending, and overdue.
CREATE INDEX waits_pending_by_deadline ON waits (deadline) WHERE NOT satisfied;
