-- H3: an evaluation run is about one experiment, and the run row says which.
--
-- Before this, an evaluation run carried `experiments:halt` for the whole tenant. Text
-- it read - another experiment's hypothesis, written on a drafting turn - could name a
-- different live experiment, and the halt that followed was granted. The subject is
-- what narrows the envelope (layer 8), and it is a column because a run is resumed in
-- whichever process picks it up: a binding held only in memory would be lost exactly
-- when the run moved.
--
-- The experiment id, not a resource path: the trigger that woke the run names an
-- experiment, and turning it into a resource is layer 8's job, done in one place.
-- NULL means unbound - every run before this migration, and every draft run, which
-- may name an experiment nobody has written yet.

ALTER TABLE runs ADD COLUMN subject text;

ALTER TABLE runs ADD CONSTRAINT run_subject_is_an_identifier
    CHECK (subject IS NULL OR (btrim(subject) <> '' AND strpos(subject, '/') = 0));

-- An evaluation run woken by a trigger already has its subject on record: the cycle it
-- settled names the experiment, and the trigger is the trusted source for it. Copy it
-- rather than leave those runs unbound.
UPDATE runs
   SET subject = trigger_cycles.experiment_id
  FROM trigger_cycles
 WHERE trigger_cycles.run_id = runs.run_id
   AND runs.stage = 'evaluation'
   AND strpos(trigger_cycles.experiment_id, '/') = 0;

-- Every new evaluation run names its subject. NOT VALID, so an evaluation row nothing
-- could backfill does not fail the deploy: it stays, and the code refuses to load it
-- (`Run` raises) rather than resuming it with tenant-wide halt authority. Failing
-- closed on one stranded row is the cheaper mistake.
ALTER TABLE runs ADD CONSTRAINT evaluation_runs_name_their_subject
    CHECK (stage <> 'evaluation' OR subject IS NOT NULL) NOT VALID;
