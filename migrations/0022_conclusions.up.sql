-- L5: what a measured experiment concluded, once per endpoint.
--
-- The conclude step is at-least-once (a durable timer, then an activity), so the record is
-- keyed on (tenant, experiment, version, endpoint) and a second write is the same
-- conclusion, not another. `endpoint` is 0 for the first look and 1 after the one allowed
-- extension. Insert-only: a conclusion that could be edited after a person acted on it would
-- make the action about something else.
--
-- `simulated` has no default (ADR-0014): a row that forgot to say it was planted would read
-- as a result.

CREATE TABLE conclusions (
    tenant             text        NOT NULL,
    experiment_id      text        NOT NULL,
    experiment_version text        NOT NULL,
    endpoint           integer     NOT NULL,
    action             text        NOT NULL,
    reason             text        NOT NULL,
    simulated          boolean     NOT NULL,
    -- The Readout and the rule it was decided under, whole.
    readout            jsonb       NOT NULL,
    rule               jsonb       NOT NULL,
    concluded_at       timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant, experiment_id, experiment_version, endpoint),
    CONSTRAINT a_conclusion_is_a_known_action CHECK (action IN ('promote', 'stop', 'extend')),
    CONSTRAINT endpoint_is_not_negative CHECK (endpoint >= 0)
);

CREATE FUNCTION conclusions_are_final() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is insert-only: a conclusion is never edited after it is recorded',
        TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation',
              CONSTRAINT = 'conclusions_are_final';
END
$$;

CREATE TRIGGER conclusions_insert_only
    BEFORE UPDATE OR DELETE ON conclusions
    FOR EACH ROW EXECUTE FUNCTION conclusions_are_final();
