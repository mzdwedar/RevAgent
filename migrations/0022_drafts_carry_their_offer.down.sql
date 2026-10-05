ALTER TABLE experiment_versions
    DROP CONSTRAINT an_offer_is_whole_and_costed,
    DROP COLUMN total_cost_cents,
    DROP COLUMN cost_per_customer_cents,
    DROP COLUMN offer_months,
    DROP COLUMN offer_amount,
    DROP COLUMN offer_kind;
