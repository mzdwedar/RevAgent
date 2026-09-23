-- Criterion 24: a replayed interaction never reaches the approval store.
--
-- A signature proves a payload came from Slack. It does not prove it arrived once.
-- Anyone who can observe a request can send it again, and a replayed "Approve" inside
-- the freshness window is a valid message that must still be refused - the second copy
-- is not a second decision.
--
-- Keyed on the signature: an exact replay carries an identical one, and changing the
-- timestamp to get a new signature needs the signing secret.

CREATE TABLE slack_deliveries (
    signature   text        PRIMARY KEY,
    sent_at     text        NOT NULL,
    received_at timestamptz NOT NULL DEFAULT now()
);

-- Rows only matter inside the freshness window; older ones are prunable.
CREATE INDEX slack_deliveries_by_age ON slack_deliveries (received_at);
