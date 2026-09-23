-- Part 4: at-least-once delivery meets an idempotent cycle (criterion 3).
--
-- A trigger broker redelivers. Without a claim, a redelivered trigger scores the cohort
-- again, drafts again, and asks a human again about a decision they already made.

CREATE TABLE trigger_cycles (
    experiment_id text        NOT NULL,
    data_as_of    text        NOT NULL,
    kind          text        NOT NULL,
    outcome       text,
    run_id        text,
    claimed_at    timestamptz NOT NULL DEFAULT now(),
    settled_at    timestamptz,

    -- Keyed on kind as well as watermark, which is a deliberate refinement of the
    -- spec's "keyed on data_as_of".
    --
    -- The hazard being prevented is a broker redelivering *the same message*, and a
    -- redelivery carries the same kind, so this key covers it. Dropping `kind` would
    -- also collapse a `data_arrival` into an earlier `metric_movement` at the same
    -- watermark - and those two do not carry the same authority. The one that may
    -- propose would be silently suppressed by the one that may not, which is an
    -- authority downgrade arriving as a deduplication.
    PRIMARY KEY (experiment_id, data_as_of, kind),

    CONSTRAINT cycle_kind_is_known CHECK (kind IN ('data_arrival', 'metric_movement')),

    -- Settled means "this cycle reached an outcome". An outcome with no time, or a time
    -- with no outcome, is a cycle nobody can read.
    CONSTRAINT settled_cycles_record_both CHECK ((outcome IS NULL) = (settled_at IS NULL)),

    -- The asymmetry, held by the database as well as by policy. A `metric_movement`
    -- cycle that recorded a propose would be an optional-stopping machine with a row
    -- to prove it.
    CONSTRAINT metric_movement_never_proposes
        CHECK (NOT (kind = 'metric_movement' AND outcome = 'propose'))
);

-- What `operator stalled` works through: claimed, never settled.
CREATE INDEX trigger_cycles_unsettled ON trigger_cycles (claimed_at) WHERE outcome IS NULL;
