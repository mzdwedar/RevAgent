-- SPEC-durable-runtime.md, T37: which run is an experiment's.
--
-- SPEC.md: one durable run per experiment, living for weeks, fed by triggers. A trigger
-- names an experiment, and Temporal needs a workflow id, which is the run's. This is the
-- record that joins the two. Temporal owns where the run is; this says which run it is.
--
-- The first trigger for an experiment claims the row with ids minted beforehand, and
-- only the winner's ids are ever written anywhere, so two first deliveries racing
-- produce one session and one run rather than an orphan. For the same reason there is
-- no foreign key onto `runs` or `sessions`: the claim comes before either exists.

CREATE TABLE experiment_runs (
    tenant        text        NOT NULL,
    experiment_id text        NOT NULL,
    run_id        text        NOT NULL UNIQUE,
    session_id    text        NOT NULL UNIQUE,
    claimed_at    timestamptz NOT NULL DEFAULT now(),

    -- The same key as `experiments`: `exp-7` at two tenants is two experiments.
    PRIMARY KEY (tenant, experiment_id)
);
