-- Memory and its maintenance queue were Python lists: a restart forgot every explicit
-- write, which makes "persist this on purpose, with a TTL" a promise about a process
-- rather than about the memory. Both move to the substrate.
--
-- The shape is the record's shape, not a document: scope, provenance, freshness (written
-- at + ttl) and trust are columns so recall can be scoped in the query, and so a record
-- cannot be stored without them. The write door (`context.memory.write`) is still the
-- only caller; the CHECKs are the second lock, not a replacement for it.
CREATE TABLE memory_records (
    id          bigserial PRIMARY KEY,
    key         text        NOT NULL,
    value       text        NOT NULL,
    tenant      text        NOT NULL CHECK (tenant <> ''),
    user_id     text,
    session_id  text,
    project     text,
    provenance  text        NOT NULL CHECK (provenance <> ''),
    written_at  timestamptz NOT NULL,
    ttl         interval    NOT NULL CHECK (ttl > interval '0'),
    trust       text        NOT NULL CHECK (trust IN ('first_party', 'untrusted'))
);
CREATE INDEX memory_records_by_tenant ON memory_records (tenant, id);

CREATE TABLE maintenance_jobs (
    id          bigserial PRIMARY KEY,
    job         text        NOT NULL,
    subject     text        NOT NULL,
    enqueued_at timestamptz NOT NULL DEFAULT now()
);
