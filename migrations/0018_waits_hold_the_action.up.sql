-- A1: what a person is asked about is what commits.
--
-- H4. An approval wait recorded the *fingerprint* of what it asked about (0009), and the
-- action itself lived only in the LangGraph checkpoint of the turn that proposed it. The
-- commit rebuilt it from there, so the first checkpoint change the commit could not read
-- turned every parked approval into "nothing approved" and lost the person's answer.
-- The wait now holds the prepared action - the tool and its validated arguments - so the
-- commit reads it from the record, validates it against the tool's schema again, and
-- holds it to the fingerprint beside it. The checkpoint says where a turn stopped; it is
-- not the record of what anyone agreed to.
ALTER TABLE waits ADD COLUMN action_tool text;
ALTER TABLE waits ADD COLUMN action_arguments jsonb;

-- Both or neither. Not required of approval waits parked before this version: a wait
-- without its action is refused at the act, and audited, rather than backfilled with a
-- guess. (A CHECK that failed old rows would also refuse satisfying them.)
ALTER TABLE waits ADD CONSTRAINT recorded_actions_are_whole CHECK (
    (action_tool IS NULL) = (action_arguments IS NULL)
);

-- H3. A person's answer, and a refusal at the act that never reached the gateway, are
-- accountability records. Until now the only row was the gateway's, under the agent's
-- principal, so a considered "no", a forged wake-up and an outsider looked the same.
-- They now say which wait they were about and the state it was asked against. Nullable:
-- a gateway record is about an action, not a wait.
ALTER TABLE audit.records ADD COLUMN wait_id text;
ALTER TABLE audit.records ADD COLUMN state_snapshot text;

CREATE INDEX audit_records_by_wait ON audit.records (wait_id, id) WHERE wait_id IS NOT NULL;
