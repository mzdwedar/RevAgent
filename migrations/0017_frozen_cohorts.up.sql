-- T40b: the frozen cohort a proposal rests on, recorded when its cycle settles.
--
-- A `propose` cycle freezes a cohort and names it with an experiment version. Until now
-- only the name was kept (trigger_cycles.run_id), and everything the rest of the run
-- needs was thrown away with the process that scored it:
--
--   * the rollout's own arguments: the cohort predicate (targeting model version, risk
--     threshold) travels in the rollout payload, because the rollout is *to this cohort*;
--   * what the approver is shown: how many customers and how much revenue is at risk.
--     An approval nobody could size is not an approval (approval legibility).
--
-- Written after layer 8 authorised the proposal and before the cycle settles
-- (cycles.evaluate_to_settled), so a refused proposal leaves no row, and a death between
-- the two is finished by the rerun. Insert-only: a version names one cohort, so the
-- same version twice is the same row, and it never changes after it is written.

CREATE TABLE frozen_cohorts (
    tenant                     text             NOT NULL,
    experiment_id              text             NOT NULL,
    experiment_version         text             NOT NULL,
    data_as_of                 text             NOT NULL,
    targeting_model_version    text             NOT NULL,
    risk_threshold             double precision NOT NULL,
    size                       integer          NOT NULL,
    annual_value_at_risk_cents bigint           NOT NULL,
    -- `Cohort.description()`, whole: what is recorded on the experiment and shown to
    -- an approver. The columns above are the parts code reads.
    description                jsonb            NOT NULL,
    frozen_at                  timestamptz      NOT NULL DEFAULT now(),

    PRIMARY KEY (tenant, experiment_id, experiment_version),
    CONSTRAINT a_cohort_has_members CHECK (size > 0),
    CONSTRAINT value_at_risk_is_not_negative CHECK (annual_value_at_risk_cents >= 0)
);

-- Insert-only, held by the database: a frozen cohort that could be edited after an
-- approver saw it would make the approval about something else.
CREATE FUNCTION frozen_cohorts_are_frozen() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION '% is insert-only: a frozen cohort is never edited after it is recorded',
        TG_TABLE_NAME
        USING ERRCODE = 'integrity_constraint_violation',
              CONSTRAINT = 'frozen_cohorts_are_frozen';
END
$$;

CREATE TRIGGER frozen_cohorts_insert_only
    BEFORE UPDATE OR DELETE ON frozen_cohorts
    FOR EACH ROW EXECUTE FUNCTION frozen_cohorts_are_frozen();
