-- Part 3: a session is an isolation boundary, and the three stores stay three stores.
--
-- Three tables, not one with a `kind` column. The transcript is the canonical record
-- of what occurred; the working state is a mutable scratchpad that can be rewound;
-- memory is neither and does not live here at all. Merging them is the Part 5 failure
-- mode, and a schema is a good place to make merging them inconvenient.

CREATE TABLE sessions (
    session_id text        PRIMARY KEY,
    user_id    text        NOT NULL,
    tenant     text        NOT NULL,
    created_at timestamptz NOT NULL,

    -- The invariant, held by the database rather than only by a constructor.
    -- Keying a session on the user id makes two unrelated tasks share a transcript
    -- and a scratchpad; a CHECK survives a code path that forgot to ask.
    CONSTRAINT session_id_is_not_the_user_id CHECK (session_id <> user_id)
);

CREATE INDEX sessions_by_owner ON sessions (tenant, user_id);

CREATE TABLE transcript_events (
    id         bigserial   PRIMARY KEY,
    session_id text        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    kind       text        NOT NULL,
    body       text        NOT NULL,
    at         timestamptz NOT NULL DEFAULT now()
);

-- Append-only and read in order. The id, not the timestamp, is the order: two events
-- in the same millisecond still have an answer.
CREATE INDEX transcript_events_by_session ON transcript_events (session_id, id);

CREATE TABLE working_state (
    session_id text        NOT NULL REFERENCES sessions (session_id) ON DELETE CASCADE,
    key        text        NOT NULL,
    value      jsonb       NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),

    PRIMARY KEY (session_id, key)
);
