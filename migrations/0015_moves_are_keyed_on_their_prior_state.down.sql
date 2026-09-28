-- Back to 0012's identities. This fails, rather than deleting history, on a registry
-- where a target was legitimately reached twice - a wording revisited, or the same move
-- repeated with an identical payload - because 0012's indexes cannot represent that.
-- Which is the defect 0015 fixed; rolling back past it on such a registry needs a
-- decision about which record to lose, and a migration should not make it.
CREATE UNIQUE INDEX one_row_per_revision
    ON draft_revisions (tenant, experiment_id, experiment_version, md5(hypothesis));

DROP INDEX one_ending_per_version;
DROP INDEX one_abstention_per_prior;
DROP INDEX one_rollout_per_prior;

CREATE UNIQUE INDEX one_row_per_effect
    ON registry_events (tenant, experiment_id, experiment_version, kind, md5(payload::text));

ALTER TABLE registry_events DROP CONSTRAINT a_move_names_where_it_started;
ALTER TABLE registry_events DROP COLUMN follows;
