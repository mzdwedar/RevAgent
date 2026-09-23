-- Criterion 25: who may approve, decided in layer 8.
--
-- Membership is per tenant, and the primary key says so. A person trusted to approve
-- tenant A's rollouts has no standing over tenant B's, and the only way to gain it is
-- a row someone added on purpose. Default deny falls out of the schema rather than
-- being a branch somebody has to remember to write.

CREATE TABLE approvers (
    tenant        text        NOT NULL,
    -- The identity Slack asserts. A claim until this table is consulted.
    slack_user_id text        NOT NULL,
    -- What the audit trail records. Separate from the Slack id because "who approved
    -- this refund" should still be answerable after someone leaves the workspace and
    -- their id is recycled.
    principal     text        NOT NULL,
    added_at      timestamptz NOT NULL DEFAULT now(),
    added_by      text        NOT NULL,

    PRIMARY KEY (tenant, slack_user_id),

    CONSTRAINT approver_names_a_principal CHECK (btrim(principal) <> ''),
    CONSTRAINT approver_records_who_added_them CHECK (btrim(added_by) <> '')
);

CREATE INDEX approvers_by_principal ON approvers (principal);
