-- D2 (ADR-0015): the model chooses the offer; the registry records it with its cost.
--
-- The offer is three flat scalars, and the cost is computed by the registry from the
-- frozen cohort and the policy bounds - never supplied by the model - so what an approver
-- is later shown is a figure nobody argued into the row. All nullable together: drafts
-- written before the model chose offers have none. experiment_versions stays insert-only
-- (the trigger guards UPDATE and DELETE; these columns are written with the row).

ALTER TABLE experiment_versions
    ADD COLUMN offer_kind              text,
    ADD COLUMN offer_amount            integer,
    ADD COLUMN offer_months            integer,
    ADD COLUMN cost_per_customer_cents bigint,
    ADD COLUMN total_cost_cents        bigint,
    ADD CONSTRAINT an_offer_is_whole_and_costed CHECK (
        (offer_kind IS NULL AND offer_amount IS NULL AND offer_months IS NULL
            AND cost_per_customer_cents IS NULL AND total_cost_cents IS NULL)
        OR
        (offer_kind IS NOT NULL AND offer_amount >= 1 AND offer_months >= 1
            AND cost_per_customer_cents >= 0 AND total_cost_cents >= 0)
    );
