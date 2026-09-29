ALTER TABLE waits DROP CONSTRAINT reconcile_waits_name_their_claim;
ALTER TABLE waits DROP CONSTRAINT pending_reconcile_waits_have_a_deadline;
ALTER TABLE waits DROP COLUMN idempotency_key;
