-- Parts 7, 8: what a human authorised, what was claimed before it was committed, and
-- what the system is accountable for afterwards.

CREATE TABLE approvals (
    id                 text        PRIMARY KEY,
    -- Ordering, because two approvals granted in the same microsecond still have a
    -- "most recent" and `find` has to agree with itself about which.
    seq                bigserial   NOT NULL UNIQUE,
    run_id             text        NOT NULL REFERENCES runs (run_id) ON DELETE CASCADE,
    action_fingerprint text        NOT NULL,
    state_snapshot     text        NOT NULL,
    approver           text        NOT NULL,
    granted_at         timestamptz NOT NULL,
    summary            text        NOT NULL,

    -- The two ValueErrors in `grant`, restated where they cannot be bypassed. An
    -- empty summary records agreement to a blank screen; an unnamed approver leaves
    -- an audit trail that cannot answer who decided.
    CONSTRAINT approval_names_an_approver CHECK (btrim(approver) <> ''),
    CONSTRAINT approval_records_what_was_shown CHECK (btrim(summary) <> '')
);

CREATE INDEX approvals_by_action ON approvals (run_id, action_fingerprint, seq);

CREATE TABLE idempotency_claims (
    key        text        PRIMARY KEY,
    claimed_at timestamptz NOT NULL,
    receipt    text,
    settled_at timestamptz,

    -- Settled means "the surface answered, and this is what it said". A receipt with
    -- no time, or a time with no receipt, is a half-recorded outcome.
    CONSTRAINT settled_claims_record_both CHECK ((receipt IS NULL) = (settled_at IS NULL))
);

-- What a reconciliation job works through: claimed, never answered.
CREATE INDEX idempotency_unresolved ON idempotency_claims (claimed_at) WHERE receipt IS NULL;

-- A schema of its own, so that the access rules and retention an audit trail needs can
-- differ from the operational tables without a migration to move it later. Granting
-- read on `public` should not grant read on this.
CREATE SCHEMA audit;

CREATE TABLE audit.records (
    id                 bigserial   PRIMARY KEY,
    -- Deliberately NOT a foreign key onto runs.
    --
    -- Every other table here cascades from sessions: delete the session and the
    -- operational record goes with it. An audit record must not. "We deleted the
    -- session" is not an answer to "who authorised this refund", and a retention
    -- policy that erases accountability as a side effect of tidying up is the Part 8
    -- boundary collapsing - audit treated as a debug log with a longer TTL.
    run_id             text        NOT NULL,
    principal          text        NOT NULL,
    tenant             text        NOT NULL,
    action_fingerprint text        NOT NULL,
    surface            text        NOT NULL,
    resource           text        NOT NULL,
    policy_decision    text        NOT NULL,
    approval_id        text,
    outcome            text        NOT NULL,
    at                 timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX audit_records_by_run ON audit.records (run_id, id);
CREATE INDEX audit_records_by_tenant ON audit.records (tenant, at);
