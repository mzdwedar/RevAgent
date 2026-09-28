-- Audit C2: a write is identified by the state it moves from, not by where it lands.
--
-- 0012 held unique what a write's key was made of: kind + payload per version
-- (`one_row_per_effect`), wording per version (`one_row_per_revision`). Both say "the
-- same target twice is one effect", and a target can legitimately be reached twice. A
-- rollout to 10%, then 25%, then back to 10% - approved by a person each time - collided
-- with the first, and exposure stayed at 25% while the run reported success. A draft
-- reworded A, B, A stayed at B. A later cycle that abstained for the same reason as an
-- earlier one left no record.
--
-- The identity that does not repeat is (where it started, where it went). 0012 is
-- applied and immutable, so the fix goes forward here.

-- The event a rollout or an abstention moved from: for a rollout, the latest rollout
-- before it (0 for the first); for an abstention, the latest event of any kind (0 if
-- none). Halts and discards leave it NULL: each ends a version, which the status guard
-- already makes once-only, and the index below says so on its own.
ALTER TABLE registry_events ADD COLUMN follows bigint;

-- NOT VALID: the rule is for every row written from here on. Rows 0012 wrote were keyed
-- the old way and are history, which is insert-only - nobody rewrites it to fit a newer
-- rule. A NULL is distinct in a unique index, so they never collide with a new row.
ALTER TABLE registry_events
    ADD CONSTRAINT a_move_names_where_it_started
        CHECK (kind NOT IN ('rollout', 'abstention') OR follows IS NOT NULL) NOT VALID;

DROP INDEX one_row_per_effect;

-- One rollout from each prior rollout, per experiment: history is a line, never a fork.
-- The surface also checks, in the statement that writes, that the prior it names is
-- still the latest. Under read committed two racing statements can both see the same
-- latest, and this is what refuses the second.
CREATE UNIQUE INDEX one_rollout_per_prior
    ON registry_events (tenant, experiment_id, follows) WHERE kind = 'rollout';

-- One abstention per thing an evaluation read. Two cycles that read the same record
-- and abstained are one cycle recorded twice.
CREATE UNIQUE INDEX one_abstention_per_prior
    ON registry_events (tenant, experiment_id, follows) WHERE kind = 'abstention';

-- A halt or a discard ends a version, once. Stronger than the payload identity it
-- replaces, which let two halts with different reasons be two rows had the status
-- guard ever let them through.
CREATE UNIQUE INDEX one_ending_per_version
    ON registry_events (tenant, experiment_id, experiment_version, kind)
    WHERE kind IN ('halt', 'discard');

-- A revision's prior is its `revision_no` minus one, so the primary key on
-- (version, revision_no) is already "one revision from each prior", and the wording
-- is free to come back.
DROP INDEX one_row_per_revision;
