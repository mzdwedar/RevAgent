-- C1: an exposure event says how much exposure, in the store as well as in the client.
--
-- A rollout written with no `percentage` - or with 150, or with "all" - is a row the
-- history read cannot derive current exposure from, and a halt naming 5% is a rollout
-- recorded under a halt's name. Both clients refuse those payloads before they write
-- (`execution/surfaces.py`, `_shaped`); these CHECKs are the store's copy, because the
-- next client - or an operator with psql - would not know the convention. The same
-- reasoning as `registry_history_is_insert_only`.
--
-- Written against the text form of the JSON number, not a cast: a CHECK's `AND` does
-- not promise to short-circuit, so `(payload->>'percentage')::int` could raise on a
-- string before the type test excluded it. `coalesce(..., false)` because a CHECK that
-- evaluates to NULL passes, and a missing key is exactly the NULL case.
--
-- Validated, not `NOT VALID`: 0012 has not shipped beyond this branch, so there is no
-- existing row these could strand. A store that did hold one should fail this
-- migration loudly rather than keep a history nothing can read.

ALTER TABLE registry_events
    ADD CONSTRAINT rollout_names_a_whole_percentage CHECK (
        kind <> 'rollout'
        OR coalesce(
            jsonb_typeof(payload -> 'percentage') = 'number'
                AND payload ->> 'percentage' ~ '^(100|[1-9]?[0-9])$',
            false
        )
    ),
    ADD CONSTRAINT halt_names_zero CHECK (
        kind <> 'halt'
        OR coalesce(
            jsonb_typeof(payload -> 'percentage') = 'number'
                AND payload ->> 'percentage' = '0',
            false
        )
    );
