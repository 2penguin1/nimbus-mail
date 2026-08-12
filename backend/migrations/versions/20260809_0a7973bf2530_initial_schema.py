"""initial schema

The whole of docs/HLD.md §8: tenancy, the storage engine, messages, search,
provisioning, and the replay guard. Plus the refcount trigger, which is the one piece
that must exist or the storage engine silently stops reclaiming space.

The SQL is embedded rather than read from a file on purpose. A migration is a record of
what was already applied — it has to keep producing the same result forever, so it must
not depend on a file that could later change.

Revision ID: 0a7973bf2530
Revises:
Create Date: 2026-08-09

"""
from typing import Sequence, Union

from alembic import op

revision: str = "0a7973bf2530"
down_revision: Union[str, Sequence[str], None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gives us gen_random_uuid()


-- =====================================================================
--  TENANCY
-- =====================================================================

CREATE TABLE reseller (
    id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    name          TEXT        NOT NULL,
    api_key_hash  TEXT        NOT NULL UNIQUE,
    webhook_url   TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE domain (
    id           UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id  UUID        NOT NULL REFERENCES reseller(id) ON DELETE CASCADE,
    name         TEXT        NOT NULL UNIQUE,          -- "acme.com"
    verified     BOOLEAN     NOT NULL DEFAULT false,
    -- FK added after `mailbox` exists, because the two tables point at each other.
    catch_all_mailbox_id UUID,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE mailbox (
    id             UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id      UUID        NOT NULL REFERENCES domain(id) ON DELETE CASCADE,
    local_part     TEXT        NOT NULL,               -- "sujal" in sujal@acme.com
    -- NULL for shared mailboxes: nobody logs into them, members read them.
    password_hash  TEXT,
    quota_bytes    BIGINT      NOT NULL DEFAULT 32212254720,   -- 30 GB
    plan           TEXT        NOT NULL DEFAULT '30GB',
    is_shared      BOOLEAN     NOT NULL DEFAULT false,
    -- Logical usage (what the user thinks they used). HLD §11.
    -- Maintained inside the delivery/delete transaction, never by scanning.
    used_bytes     BIGINT      NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (domain_id, local_part)
);

ALTER TABLE domain
    ADD CONSTRAINT domain_catch_all_fk
    FOREIGN KEY (catch_all_mailbox_id) REFERENCES mailbox(id) ON DELETE SET NULL;

CREATE TABLE alias (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    domain_id         UUID NOT NULL REFERENCES domain(id) ON DELETE CASCADE,
    local_part        TEXT NOT NULL,
    target_mailbox_id UUID NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    UNIQUE (domain_id, local_part)
);

CREATE TABLE shared_mailbox_member (
    shared_mailbox_id UUID NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    member_mailbox_id UUID NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    PRIMARY KEY (shared_mailbox_id, member_mailbox_id)
);

CREATE TABLE forwarding_rule (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    mailbox_id      UUID    NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    forward_to_addr TEXT    NOT NULL,
    keep_copy       BOOLEAN NOT NULL DEFAULT true
);


-- =====================================================================
--  STORAGE ENGINE  — the heart of the project
-- =====================================================================
--
-- Dedup is scoped PER RESELLER, never globally. HLD §12: with one global row, an
-- instant upload would reveal that somebody else already has that exact file. That
-- is a real published attack against Dropbox. reseller_id is part of every key here.
--
-- RESTRICT, not CASCADE, on reseller_id. A cascade would delete every blob row — and
-- with them every s3_key — leaving the objects in S3 with nothing that knows they
-- exist. Deleting a reseller must be staged: empty the mailboxes, let refcounts fall
-- to zero, let GC delete the objects, and only then remove the reseller.

CREATE TABLE blob (
    reseller_id  UUID        NOT NULL REFERENCES reseller(id) ON DELETE RESTRICT,
    hash         CHAR(64)    NOT NULL,          -- SHA-256 of the whole attachment
    size_bytes   BIGINT      NOT NULL,
    chunk_count  INT         NOT NULL,
    -- Counts mailbox_message rows that reach this blob, NOT messages.
    refcount     BIGINT      NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (reseller_id, hash)
);

CREATE TABLE chunk (
    reseller_id  UUID        NOT NULL REFERENCES reseller(id) ON DELETE RESTRICT,
    hash         CHAR(64)    NOT NULL,          -- SHA-256 of this 4 MB piece
    size_bytes   INT         NOT NULL,
    s3_key       TEXT        NOT NULL,
    -- Counts blob_chunk rows. A chunk shared by two blobs survives either being deleted.
    refcount     BIGINT      NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (reseller_id, hash)
);

CREATE TABLE blob_chunk (
    reseller_id  UUID     NOT NULL,
    blob_hash    CHAR(64) NOT NULL,
    seq          INT      NOT NULL,
    chunk_hash   CHAR(64) NOT NULL,
    PRIMARY KEY (reseller_id, blob_hash, seq),
    FOREIGN KEY (reseller_id, blob_hash)  REFERENCES blob(reseller_id, hash)  ON DELETE CASCADE,
    FOREIGN KEY (reseller_id, chunk_hash) REFERENCES chunk(reseller_id, hash)
);

-- GC only looks for refcount = 0, so index only those rows. A partial index stays
-- tiny instead of covering millions of live blobs.
CREATE INDEX blob_gc_idx  ON blob (created_at)  WHERE refcount = 0;
CREATE INDEX chunk_gc_idx ON chunk (created_at) WHERE refcount = 0;


-- =====================================================================
--  MESSAGES
-- =====================================================================

CREATE TABLE message (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    -- RESTRICT for the same reason as blob: raw_s3_key points at a real S3 object.
    reseller_id       UUID        NOT NULL REFERENCES reseller(id) ON DELETE RESTRICT,
    raw_s3_key        TEXT        NOT NULL,
    message_id_header TEXT,
    in_reply_to       TEXT,                     -- used to derive thread_id
    subject           TEXT,
    from_addr         TEXT,
    sent_at           TIMESTAMPTZ,              -- what the Date: header claims
    received_at       TIMESTAMPTZ NOT NULL DEFAULT now(),   -- when WE got it
    size_bytes        BIGINT      NOT NULL DEFAULT 0,
    thread_id         UUID
);

CREATE INDEX message_thread_idx ON message (thread_id);

CREATE TABLE message_attachment (
    message_id   UUID     NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    blob_hash    CHAR(64) NOT NULL,
    reseller_id  UUID     NOT NULL,
    filename     TEXT,
    content_type TEXT,
    size_bytes   BIGINT   NOT NULL,
    PRIMARY KEY (message_id, blob_hash),
    FOREIGN KEY (reseller_id, blob_hash) REFERENCES blob(reseller_id, hash)
);

-- THE FAN-OUT TABLE. 40 recipients = 40 rows of ~40 bytes, 1 message, 1 blob.
CREATE TABLE mailbox_message (
    mailbox_id   UUID        NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    message_id   UUID        NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    folder       TEXT        NOT NULL DEFAULT 'inbox',
    is_read      BOOLEAN     NOT NULL DEFAULT false,
    is_snoozed   BOOLEAN     NOT NULL DEFAULT false,
    snooze_until TIMESTAMPTZ,
    received_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (mailbox_id, message_id)
);

-- Listing an inbox is the most common query in the whole system.
CREATE INDEX mailbox_message_list_idx
    ON mailbox_message (mailbox_id, folder, received_at DESC);


-- =====================================================================
--  SEARCH
-- =====================================================================

CREATE TABLE message_index (
    message_id UUID NOT NULL REFERENCES message(id) ON DELETE CASCADE,
    mailbox_id UUID NOT NULL REFERENCES mailbox(id) ON DELETE CASCADE,
    tsv        TSVECTOR NOT NULL,
    PRIMARY KEY (mailbox_id, message_id)
);

CREATE INDEX message_index_tsv_idx ON message_index USING GIN (tsv);


-- =====================================================================
--  PROVISIONING
-- =====================================================================

CREATE TABLE provision_order (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    reseller_id     UUID        NOT NULL REFERENCES reseller(id) ON DELETE CASCADE,
    -- A retry with the same key returns the ORIGINAL result. Never creates twice.
    idempotency_key TEXT        NOT NULL UNIQUE,
    status          TEXT        NOT NULL DEFAULT 'pending',
    payload         JSONB       NOT NULL,     -- what was asked for
    result          JSONB,                    -- what was created, replayed on retry
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =====================================================================
--  EXACTLY-ONCE PROCESSING
-- =====================================================================
--
-- Kafka delivers at-least-once, so the worker WILL see the same event twice.
-- Inserting here is the first statement of the delivery transaction. A replay hits
-- this primary key, the transaction aborts, and nothing is written twice.

CREATE TABLE processed_event (
    event_key    TEXT PRIMARY KEY,             -- the raw message's s3_key
    message_id   UUID REFERENCES message(id) ON DELETE SET NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);


-- =====================================================================
--  FOREIGN KEY INDEXES
-- =====================================================================
--
-- Postgres indexes primary keys and UNIQUE constraints automatically. It does NOT
-- index foreign keys. Without an index, deleting a parent means scanning the whole
-- child table.
--
-- We do not index every foreign key — each one costs a write on every insert. Only
-- the delete paths that actually run, where the child table is big.

-- Runs on EVERY GC sweep. These two are the hot ones.
CREATE INDEX message_attachment_blob_idx ON message_attachment (reseller_id, blob_hash);
CREATE INDEX blob_chunk_chunk_idx        ON blob_chunk (reseller_id, chunk_hash);

-- Runs when a domain or mailbox is deprovisioned.
CREATE INDEX mailbox_domain_idx          ON mailbox (domain_id);
CREATE INDEX alias_target_idx            ON alias (target_mailbox_id);
CREATE INDEX shared_member_idx           ON shared_mailbox_member (member_mailbox_id);
CREATE INDEX forwarding_rule_mailbox_idx ON forwarding_rule (mailbox_id);

-- Runs when a message is deleted. mailbox_message's primary key starts with
-- mailbox_id, so it cannot answer "find every row for this message".
CREATE INDEX mailbox_message_msg_idx     ON mailbox_message (message_id);
CREATE INDEX message_index_msg_idx       ON message_index (message_id);
CREATE INDEX processed_event_msg_idx     ON processed_event (message_id);


-- =====================================================================
--  REFCOUNT ACCOUNTING
-- =====================================================================
--
-- blob.refcount counts mailbox_message rows, so EVERY way such a row can disappear
-- must decrement it:
--
--     1. a user deletes a message
--     2. a mailbox is deprovisioned   -> CASCADE
--     3. a domain is deleted          -> CASCADE of a CASCADE
--
-- Paths 2 and 3 happen inside the database, where application code never runs. Left
-- to application code they would silently leave refcounts too high, GC would free
-- nothing, and the storage engine would quietly stop doing its one job.
--
-- One trigger covers all three. This is the only place refcounts go down.

CREATE FUNCTION mailbox_message_deleted() RETURNS TRIGGER AS $$
BEGIN
    UPDATE blob b
       SET refcount = b.refcount - 1
      FROM message_attachment ma
     WHERE ma.message_id  = OLD.message_id
       AND b.reseller_id  = ma.reseller_id
       AND b.hash         = ma.blob_hash;

    -- COALESCE because the message row may already be gone if this delete arrived
    -- as a cascade from the message itself.
    UPDATE mailbox
       SET used_bytes = GREATEST(
               0,
               used_bytes - COALESCE(
                   (SELECT size_bytes FROM message WHERE id = OLD.message_id), 0)
           )
     WHERE id = OLD.mailbox_id;

    RETURN OLD;
END;
$$ LANGUAGE plpgsql;

-- ponytail: fires once per row, so deleting a 100k-message mailbox does 100k small
-- updates. Fine for deprovisioning, which is rare and offline. If bulk deletes become
-- a hot path, replace with a statement-level trigger over the transition table.
CREATE TRIGGER mailbox_message_delete_trg
    AFTER DELETE ON mailbox_message
    FOR EACH ROW EXECUTE FUNCTION mailbox_message_deleted();
    """)


def downgrade() -> None:
    # Reverse order: triggers, then the function, then tables. Postgres drops the
    # indexes with their tables, so they need no separate statement.
    op.execute("""
DROP TRIGGER IF EXISTS mailbox_message_delete_trg ON mailbox_message;
DROP FUNCTION IF EXISTS mailbox_message_deleted();

DROP TABLE IF EXISTS processed_event;
DROP TABLE IF EXISTS provision_order;
DROP TABLE IF EXISTS message_index;
DROP TABLE IF EXISTS mailbox_message;
DROP TABLE IF EXISTS message_attachment;
DROP TABLE IF EXISTS message;
DROP TABLE IF EXISTS blob_chunk;
DROP TABLE IF EXISTS chunk;
DROP TABLE IF EXISTS blob;
DROP TABLE IF EXISTS forwarding_rule;
DROP TABLE IF EXISTS shared_mailbox_member;
DROP TABLE IF EXISTS alias;
ALTER TABLE domain DROP CONSTRAINT IF EXISTS domain_catch_all_fk;
DROP TABLE IF EXISTS mailbox;
DROP TABLE IF EXISTS domain;
DROP TABLE IF EXISTS reseller;
    """)
