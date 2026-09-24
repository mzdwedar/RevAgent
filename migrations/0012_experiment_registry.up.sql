-- SPEC-registry.md, T23: the experiment registry, ours and versioned.
--
-- Four tables, because they change at four different rates and only one of them is
-- allowed to change at all:
--
--   experiments          one row per experiment; `status` moves, and nothing else does
--   experiment_versions  the frozen candidate a version names          insert-only
--   draft_revisions      each wording of the hypothesis                 insert-only
--   registry_events      rollouts, halts, discards, abstentions         insert-only
--
-- Deliberately not hung off `sessions` or `runs`. Those cascade, and an experiment that
-- real customers saw must outlive the session that drafted it - the same reasoning as
-- `audit.records`. Who did what is the audit trail's job; this is what the registry is.

CREATE TABLE experiments (
    tenant          text        NOT NULL,
    experiment_id   text        NOT NULL,
    status          text        NOT NULL,
    current_version text        NOT NULL,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),

    -- The tenant is in the key. `exp-7` at two tenants is two experiments; without it
    -- the second tenant's draft would collide with the first's.
    PRIMARY KEY (tenant, experiment_id),

    -- Four states and no more. `approved` is not one - approval lives in `approvals`
    -- and binds an action, not an experiment - and `concluded` is iteration 2. A status
    -- no tool handles is a status no tool can leave.
    CONSTRAINT experiment_status_is_known
        CHECK (status IN ('draft', 'live', 'halted', 'discarded'))
);

CREATE TABLE experiment_versions (
    tenant             text        NOT NULL,
    experiment_id      text        NOT NULL,
    experiment_version text        NOT NULL,
    variant            text        NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant, experiment_id, experiment_version),
    CONSTRAINT version_belongs_to_an_experiment
        FOREIGN KEY (tenant, experiment_id) REFERENCES experiments (tenant, experiment_id)
);

-- Circular with the key above, and still immediate: a draft is written as one
-- statement, and a foreign key is checked at the end of the statement, when both rows
-- exist. Deferring it to commit would move the refusal outside the statement that
-- caused it, where the storage seam cannot translate it.
ALTER TABLE experiments
    ADD CONSTRAINT current_version_exists
        FOREIGN KEY (tenant, experiment_id, current_version)
        REFERENCES experiment_versions (tenant, experiment_id, experiment_version);

CREATE TABLE draft_revisions (
    tenant             text        NOT NULL,
    experiment_id      text        NOT NULL,
    experiment_version text        NOT NULL,
    revision_no        integer     NOT NULL,
    hypothesis         text        NOT NULL,
    created_at         timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant, experiment_id, experiment_version, revision_no),
    CONSTRAINT revision_belongs_to_a_version
        FOREIGN KEY (tenant, experiment_id, experiment_version)
        REFERENCES experiment_versions (tenant, experiment_id, experiment_version),
    CONSTRAINT revision_says_something CHECK (btrim(hypothesis) <> '')
);

-- The same wording twice is one revision. This is the store's copy of the revise
-- tool's idempotency key: the surface never sees the key, so it holds unique what the
-- key is made of.
CREATE UNIQUE INDEX one_row_per_revision
    ON draft_revisions (tenant, experiment_id, experiment_version, md5(hypothesis));

CREATE TABLE registry_events (
    id                 bigserial   PRIMARY KEY,
    tenant             text        NOT NULL,
    experiment_id      text        NOT NULL,
    experiment_version text        NOT NULL,
    kind               text        NOT NULL,
    payload            jsonb       NOT NULL,
    at                 timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT event_belongs_to_a_version
        FOREIGN KEY (tenant, experiment_id, experiment_version)
        REFERENCES experiment_versions (tenant, experiment_id, experiment_version),
    CONSTRAINT registry_event_kind_is_known
        CHECK (kind IN ('rollout', 'halt', 'discard', 'abstention'))
);

-- The same effect twice is one row: kind and payload, per version. `jsonb` normalises
-- key order, so its text form is a stable identity. 10% and 25% differ in payload and
-- are two rows, exactly as their idempotency keys say.
CREATE UNIQUE INDEX one_row_per_effect
    ON registry_events (tenant, experiment_id, experiment_version, kind, md5(payload::text));

-- What `get_rollout_history` reads, newest last.
CREATE INDEX registry_events_by_experiment ON registry_events (tenant, experiment_id, id);

-- History is appended to, never rewritten. Held here rather than by the one client
-- that happens to append, because the next client - or an operator with psql - would
-- not know the convention. Raised as an integrity violation carrying a constraint name,
-- so the storage seam translates it like any other refusal.
--
-- TRUNCATE does not fire row triggers, which is what lets a disposable test database
-- be emptied between tests (`truncate_all`), and is not a path the application has.
CREATE FUNCTION registry_history_is_insert_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is insert-only: registry history is appended to, never rewritten',
        TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation',
              CONSTRAINT = 'registry_history_is_insert_only';
END
$$;

CREATE TRIGGER experiment_versions_insert_only
    BEFORE UPDATE OR DELETE ON experiment_versions
    FOR EACH ROW EXECUTE FUNCTION registry_history_is_insert_only();
CREATE TRIGGER draft_revisions_insert_only
    BEFORE UPDATE OR DELETE ON draft_revisions
    FOR EACH ROW EXECUTE FUNCTION registry_history_is_insert_only();
CREATE TRIGGER registry_events_insert_only
    BEFORE UPDATE OR DELETE ON registry_events
    FOR EACH ROW EXECUTE FUNCTION registry_history_is_insert_only();
