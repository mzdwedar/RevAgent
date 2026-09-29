ALTER TABLE registry_events
    DROP CONSTRAINT halt_names_zero,
    DROP CONSTRAINT rollout_names_a_whole_percentage;
