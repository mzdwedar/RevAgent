-- Part 4: run identity, recorded step boundaries, and waiting as persisted state.
--
-- `runs` is here although T3's acceptance names only steps and waits. A step or a
-- wait keyed on a run id that nothing records is an orphan: there is no row saying
-- which tenant or session it belongs to, and T10 has nothing to resume from. Run
-- identity is the Part 4 invariant these two tables hang off, so it lands with them.

CREATE TABLE runs (
    run_id      text        PRIMARY KEY,
    session_id  text        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    tenant      text        NOT NULL,
    -- `user` is reserved in Postgres. Quoting it everywhere is a papercut per query.
    acting_user text        NOT NULL,
    stage       text        NOT NULL,
    channel     text        NOT NULL,
    started_at  timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX runs_by_session ON runs (session_id, started_at);

CREATE TABLE run_steps (
    id      bigserial   PRIMARY KEY,
    run_id  text        NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    name    text        NOT NULL,
    status  text        NOT NULL,
    receipt text,
    at      timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT step_status_is_known CHECK (status IN ('started', 'completed', 'failed'))
);

CREATE INDEX run_steps_by_run ON run_steps (run_id, id);

-- The invariant, held by the database: a step completes once. A `started` row with no
-- completion is allowed and meaningful - it is a process that died mid-step, and the
-- idempotency ledger is what makes the re-run safe.
CREATE UNIQUE INDEX run_steps_complete_once
    ON run_steps (run_id, name)
    WHERE status = 'completed';

CREATE TABLE waits (
    wait_id        text        PRIMARY KEY,
    run_id         text        NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    kind           text        NOT NULL,
    state_snapshot text        NOT NULL,
    created_at     timestamptz NOT NULL,
    satisfied      boolean     NOT NULL DEFAULT false,
    satisfied_at   timestamptz,
    payload        jsonb       NOT NULL DEFAULT '{}'::jsonb,

    -- Satisfied and unsatisfied are not two independently settable columns. A wait
    -- marked satisfied with no time is a resume nobody can audit.
    CONSTRAINT satisfied_waits_record_when CHECK (satisfied = (satisfied_at IS NOT NULL))
);

-- The hot read is "does this run have a pending wait", asked at the top of every turn.
CREATE INDEX waits_pending_by_run ON waits (run_id) WHERE NOT satisfied;
