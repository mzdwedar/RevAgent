-- Audit finding H5: a reconcile wait had no deadline, named no claim, and nothing
-- reported it. `pending_waits_have_a_deadline` (0010) exempted every kind but `trigger`
-- and `human_approval`, and `reconcile` (T43) came later, so it slipped through the
-- exemption rather than being decided. A run parked on an effect that may be live (a
-- rollout customers can already see) then blocked indefinitely, with every trigger
-- queued behind it, and `operator stalled` never mentioned it.

-- Which idempotency claim the wait is about. The action fingerprint says which action;
-- the person reconciling has to settle a claim, and needs its key to do that.
ALTER TABLE waits ADD COLUMN idempotency_key text;

-- A pending reconcile wait parked before this had no deadline. Making it due now is the
-- honest backfill, as 0010 did for the other kinds: it has been waiting unwatched for as
-- long as it has been there, and a deadline in the future would hide it.
UPDATE waits SET deadline = now()
 WHERE NOT satisfied AND kind = 'reconcile' AND deadline IS NULL;

-- A constraint of its own, beside 0010's rather than replacing it: the older one is
-- unchanged, and this one closes the kind it did not name.
ALTER TABLE waits ADD CONSTRAINT pending_reconcile_waits_have_a_deadline CHECK (
    satisfied OR kind <> 'reconcile' OR deadline IS NOT NULL
);

-- A pending reconcile wait says what it is about: the action and the claim. Nothing
-- is live yet, so no pending reconcile wait should exist without a key; if one does,
-- this fails and a person looks at that claim, rather than a key being invented for it.
-- A satisfied one is history and is left as it was recorded.
ALTER TABLE waits ADD CONSTRAINT reconcile_waits_name_their_claim CHECK (
    satisfied OR kind <> 'reconcile'
    OR (action_fingerprint IS NOT NULL AND idempotency_key IS NOT NULL)
);
