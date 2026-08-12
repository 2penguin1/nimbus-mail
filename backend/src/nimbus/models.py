"""The schema as SQLAlchemy models — docs/HLD.md §8, docs/DATABASE.md.

These models describe a database that **already exists**. The migration in
`migrations/versions/` created it and remains the record of what was applied; this file
is how the application talks to it.

That order matters. `alembic revision --autogenerate` cannot emit triggers, and our
`mailbox_message` delete trigger is the only place `blob.refcount` and
`mailbox.used_bytes` go down. Regenerating the initial migration from these models
would drop it silently and GC would never free a byte again. So the migration leads and
the models follow.

**The check that keeps them honest:** `uv run alembic revision --autogenerate -m tmp`
must produce an EMPTY migration. If it produces anything, a model and the real schema
have drifted apart, and a model that lies is worse than no model at all. Delete the
temporary file afterwards.

Constraint names are spelled out rather than left to SQLAlchemy, because the database
already has Postgres's automatic names (`domain_name_key`, and so on) and the code
catches some of them by name.
"""

import datetime
import uuid

from sqlalchemy import (
    CHAR,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    PrimaryKeyConstraint,
    Text,
    UniqueConstraint,
    desc,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

# Written once here instead of on twelve columns.
_UUID = UUID(as_uuid=True)
_NEW_UUID = text("gen_random_uuid()")
_NOW = text("now()")
# SHA-256 in hex is always exactly 64 characters, so CHAR(64) — not TEXT. The fixed
# width is the point: a wrong-length hash cannot be stored at all.
_SHA256 = CHAR(64)
# TIMESTAMPTZ, not TIMESTAMP. A bare `Mapped[datetime]` gives the timezone-less kind,
# which would silently disagree with the schema and store wall-clock times with no UTC
# offset — the sort of bug that only shows up twice a year.
_TS = DateTime(timezone=True)


class Base(DeclarativeBase):
    pass


# =====================================================================
#  TENANCY
# =====================================================================

class Reseller(Base):
    """The company reselling our email, not the person reading it.

    Multi-tenancy starts here: every row in the storage engine is scoped to a reseller.
    """

    __tablename__ = "reseller"
    __table_args__ = (UniqueConstraint("api_key_hash", name="reseller_api_key_hash_key"),)

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True, server_default=_NEW_UUID)
    name: Mapped[str] = mapped_column(Text)
    api_key_hash: Mapped[str] = mapped_column(Text)
    webhook_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, server_default=_NOW)


class Domain(Base):
    __tablename__ = "domain"
    __table_args__ = (UniqueConstraint("name", name="domain_name_key"),)

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True, server_default=_NEW_UUID)
    reseller_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("reseller.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)  # "acme.com"
    verified: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # domain points at mailbox and mailbox points back at domain. Postgres cannot create
    # either FK before the other table exists, so this one is added by ALTER TABLE —
    # use_alter tells SQLAlchemy to emit it the same way.
    catch_all_mailbox_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID,
        ForeignKey("mailbox.id", ondelete="SET NULL", name="domain_catch_all_fk", use_alter=True),
    )
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, server_default=_NOW)


class Mailbox(Base):
    """One real inbox. `sujal@acme.com` is local_part 'sujal' plus its domain."""

    __tablename__ = "mailbox"
    __table_args__ = (
        UniqueConstraint("domain_id", "local_part", name="mailbox_domain_id_local_part_key"),
        Index("mailbox_domain_idx", "domain_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True, server_default=_NEW_UUID)
    domain_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("domain.id", ondelete="CASCADE")
    )
    local_part: Mapped[str] = mapped_column(Text)
    # NULL for shared mailboxes: nobody logs into them, members read them.
    password_hash: Mapped[str | None] = mapped_column(Text)
    quota_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("32212254720"))
    plan: Mapped[str] = mapped_column(Text, server_default=text("'30GB'"))
    is_shared: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Logical usage (HLD §11). Maintained in the delivery/delete transaction, never by
    # scanning messages.
    used_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, server_default=_NOW)


class Alias(Base):
    """A nickname pointing at a real mailbox. Stores no mail of its own."""

    __tablename__ = "alias"
    __table_args__ = (
        UniqueConstraint("domain_id", "local_part", name="alias_domain_id_local_part_key"),
        Index("alias_target_idx", "target_mailbox_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True, server_default=_NEW_UUID)
    domain_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("domain.id", ondelete="CASCADE")
    )
    local_part: Mapped[str] = mapped_column(Text)
    target_mailbox_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("mailbox.id", ondelete="CASCADE")
    )


class SharedMailboxMember(Base):
    """Who may read a shared inbox. The composite key stops a double-add by itself."""

    __tablename__ = "shared_mailbox_member"
    __table_args__ = (Index("shared_member_idx", "member_mailbox_id"),)

    shared_mailbox_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("mailbox.id", ondelete="CASCADE"), primary_key=True
    )
    member_mailbox_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("mailbox.id", ondelete="CASCADE"), primary_key=True
    )


class ForwardingRule(Base):
    __tablename__ = "forwarding_rule"
    __table_args__ = (Index("forwarding_rule_mailbox_idx", "mailbox_id"),)

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True, server_default=_NEW_UUID)
    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("mailbox.id", ondelete="CASCADE")
    )
    forward_to_addr: Mapped[str] = mapped_column(Text)
    keep_copy: Mapped[bool] = mapped_column(Boolean, server_default=text("true"))


# =====================================================================
#  STORAGE ENGINE — the heart of the project
# =====================================================================
#
# Dedup is scoped PER RESELLER, never globally (HLD §12). With one global row, an
# instant upload would reveal that somebody else already holds that exact file — a real
# published attack against Dropbox. That is why reseller_id is in every key here.
#
# RESTRICT, not CASCADE, on reseller_id. A cascade would delete every row holding an
# s3_key and leave the objects in S3 with nothing that knows they exist.

class Blob(Base):
    """One whole attachment, named by the SHA-256 of its bytes."""

    __tablename__ = "blob"
    __table_args__ = (
        PrimaryKeyConstraint("reseller_id", "hash"),
        # GC only ever looks for refcount = 0, so index only those rows. A partial index
        # stays tiny instead of covering millions of live blobs. It leads on
        # refcount_zeroed_at, NOT created_at: the sweep asks "how long has this been
        # garbage", and created_at answers "how long ago was it written" — see
        # migration 9c3e5a1d7b42.
        Index("blob_gc_idx", "refcount_zeroed_at", postgresql_where=text("refcount = 0")),
    )

    reseller_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("reseller.id", ondelete="RESTRICT")
    )
    hash: Mapped[str] = mapped_column(_SHA256)
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    chunk_count: Mapped[int] = mapped_column(Integer)
    # Counts mailbox_message rows that reach this blob, NOT messages. Counting messages
    # under-counts and frees data two people are still reading.
    refcount: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, server_default=_NOW)
    # When this blob BECAME garbage — the clock GC's grace period measures against.
    # Invariant: NULL exactly when refcount > 0. The delete trigger sets it, and
    # routing.deliver() clears it. Not the same as created_at, which is when the bytes
    # were first stored and never moves again.
    refcount_zeroed_at: Mapped[datetime.datetime | None] = mapped_column(
        _TS, server_default=_NOW
    )


class Chunk(Base):
    """A 4 MB piece of a blob. s3_key says where the bytes actually live."""

    __tablename__ = "chunk"
    __table_args__ = (
        PrimaryKeyConstraint("reseller_id", "hash"),
        Index("chunk_gc_idx", "created_at", postgresql_where=text("refcount = 0")),
    )

    reseller_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("reseller.id", ondelete="RESTRICT")
    )
    hash: Mapped[str] = mapped_column(_SHA256)
    size_bytes: Mapped[int] = mapped_column(Integer)
    s3_key: Mapped[str] = mapped_column(Text)
    # Counts blob_chunk rows, so a chunk shared by two blobs survives either going away.
    refcount: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, server_default=_NOW)


class BlobChunk(Base):
    """The ordered chunk map. `seq` is what reassembles a blob in the right order."""

    __tablename__ = "blob_chunk"
    __table_args__ = (
        PrimaryKeyConstraint("reseller_id", "blob_hash", "seq"),
        ForeignKeyConstraint(
            ["reseller_id", "blob_hash"], ["blob.reseller_id", "blob.hash"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["reseller_id", "chunk_hash"], ["chunk.reseller_id", "chunk.hash"]
        ),
        # Runs on every GC sweep.
        Index("blob_chunk_chunk_idx", "reseller_id", "chunk_hash"),
    )

    reseller_id: Mapped[uuid.UUID] = mapped_column(_UUID)
    blob_hash: Mapped[str] = mapped_column(_SHA256)
    seq: Mapped[int] = mapped_column(Integer)
    chunk_hash: Mapped[str] = mapped_column(_SHA256)


# =====================================================================
#  MESSAGES
# =====================================================================

class Message(Base):
    """The email itself. ONE row, no matter how many people received it."""

    __tablename__ = "message"
    __table_args__ = (
        Index("message_thread_idx", "thread_id"),
        # Block C's threading lookup: In-Reply-To -> the parent's thread_id. Leads with
        # reseller_id because that filter is a security boundary, not a speed-up —
        # Message-ID is sender-supplied and forgeable.
        Index(
            "message_thread_lookup_idx",
            "reseller_id",
            "message_id_header",
            postgresql_where=text("message_id_header IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True, server_default=_NEW_UUID)
    # RESTRICT for the same reason as blob: raw_s3_key points at a real S3 object.
    reseller_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("reseller.id", ondelete="RESTRICT")
    )
    raw_s3_key: Mapped[str] = mapped_column(Text)
    message_id_header: Mapped[str | None] = mapped_column(Text)
    in_reply_to: Mapped[str | None] = mapped_column(Text)  # used to derive thread_id
    subject: Mapped[str | None] = mapped_column(Text)
    from_addr: Mapped[str | None] = mapped_column(Text)
    # What the email SAYS. Extracted by the worker, which already has the parsed tree,
    # rather than re-parsed from S3 on every read — that costs 187 MB per 25 MB message
    # (measured) and would run inside an API request. Block F builds the search index
    # from body_text, so this is extracted once and used twice.
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    # what the Date: header claims
    sent_at: Mapped[datetime.datetime | None] = mapped_column(_TS)
    received_at: Mapped[datetime.datetime] = mapped_column(_TS, server_default=_NOW)  # when WE got it
    size_bytes: Mapped[int] = mapped_column(BigInteger, server_default=text("0"))
    thread_id: Mapped[uuid.UUID | None] = mapped_column(_UUID)


class MessageAttachment(Base):
    """Links a message to its files.

    filename lives here, not on blob: the same bytes may be `invoice.pdf` from one
    sender and `Q3-final.pdf` from another. The bytes are shared, the name is not.
    """

    __tablename__ = "message_attachment"
    __table_args__ = (
        PrimaryKeyConstraint("message_id", "blob_hash"),
        ForeignKeyConstraint(["reseller_id", "blob_hash"], ["blob.reseller_id", "blob.hash"]),
        # Runs on every GC sweep. One of the two hot indexes.
        Index("message_attachment_blob_idx", "reseller_id", "blob_hash"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("message.id", ondelete="CASCADE")
    )
    blob_hash: Mapped[str] = mapped_column(_SHA256)
    reseller_id: Mapped[uuid.UUID] = mapped_column(_UUID)
    filename: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int] = mapped_column(BigInteger)


class MailboxMessage(Base):
    """THE FAN-OUT TABLE. 40 recipients = 40 rows of ~40 bytes, 1 message, 1 blob.

    Deleting a row here fires the refcount trigger. That trigger lives in the migration,
    not in this file — see the module docstring.
    """

    __tablename__ = "mailbox_message"
    __table_args__ = (
        # Listing an inbox is the most common query in the whole system.
        Index("mailbox_message_list_idx", "mailbox_id", "folder", desc("received_at")),
        # The primary key starts with mailbox_id, so it cannot answer
        # "find every row for this message" — which is what deleting a message needs.
        Index("mailbox_message_msg_idx", "message_id"),
    )

    mailbox_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("mailbox.id", ondelete="CASCADE"), primary_key=True
    )
    message_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("message.id", ondelete="CASCADE"), primary_key=True
    )
    folder: Mapped[str] = mapped_column(Text, server_default=text("'inbox'"))
    is_read: Mapped[bool] = mapped_column(Boolean, server_default=text("false"))
    # Snoozed IS `snooze_until > now()`. There is no `is_snoozed` flag, deliberately —
    # two columns for one state can disagree, and nothing needs to run for a snooze to
    # expire. Migration c81f4e6a29d3 dropped it. NULL means "never snoozed"; a value in
    # the past means "was snoozed, and is visible again".
    snooze_until: Mapped[datetime.datetime | None] = mapped_column(_TS)
    received_at: Mapped[datetime.datetime] = mapped_column(_TS, server_default=_NOW)


# =====================================================================
#  SEARCH
# =====================================================================

class MessageIndex(Base):
    """One row per delivered copy — the same key as `mailbox_message`.

    The foreign key points at `mailbox_message`, not at `message` and `mailbox`
    separately, and that is load-bearing: deleting one copy of a message has to take its
    index row with it, or a deleted message stays findable by search. The database does
    that; no endpoint has to remember to.

    The GIN index leads with `mailbox_id` so the tenant filter is satisfied inside the
    index scan. A GIN index on `tsv` alone would make every search read a posting list
    spanning every tenant. Multicolumn GIN is usable with any subset of its columns, so
    this one index serves free-text, metadata-only and combined searches.
    """

    __tablename__ = "message_index"
    __table_args__ = (
        PrimaryKeyConstraint("mailbox_id", "message_id"),
        ForeignKeyConstraint(
            ["mailbox_id", "message_id"],
            ["mailbox_message.mailbox_id", "mailbox_message.message_id"],
            ondelete="CASCADE",
            name="message_index_mailbox_message_fkey",
        ),
        # Needs the btree_gin extension for the uuid column — see migration 4f1a9c2b8e07.
        Index("message_index_tsv_mailbox_idx", "mailbox_id", "tsv", postgresql_using="gin"),
    )

    message_id: Mapped[uuid.UUID] = mapped_column(_UUID)
    mailbox_id: Mapped[uuid.UUID] = mapped_column(_UUID)
    tsv: Mapped[str] = mapped_column(TSVECTOR)


# =====================================================================
#  PROVISIONING
# =====================================================================

class ProvisionOrder(Base):
    __tablename__ = "provision_order"
    __table_args__ = (
        # This one constraint is what makes retries safe. The API catches it by name.
        UniqueConstraint("idempotency_key", name="provision_order_idempotency_key_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(_UUID, primary_key=True, server_default=_NEW_UUID)
    reseller_id: Mapped[uuid.UUID] = mapped_column(
        _UUID, ForeignKey("reseller.id", ondelete="CASCADE")
    )
    idempotency_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, server_default=text("'pending'"))
    payload: Mapped[dict] = mapped_column(JSONB)  # what was asked for
    result: Mapped[dict | None] = mapped_column(JSONB)  # what was created, replayed on retry
    created_at: Mapped[datetime.datetime] = mapped_column(_TS, server_default=_NOW)


# =====================================================================
#  EXACTLY-ONCE PROCESSING
# =====================================================================

class ProcessedEvent(Base):
    """The table exists *for* its primary key. The collision is the feature.

    Kafka delivers at-least-once, so the worker WILL see the same event twice. Inserting
    here is the first statement of the delivery transaction; a replay hits this key, the
    transaction aborts, and nothing is written twice.
    """

    __tablename__ = "processed_event"
    __table_args__ = (Index("processed_event_msg_idx", "message_id"),)

    event_key: Mapped[str] = mapped_column(Text, primary_key=True)  # the raw message's s3_key
    message_id: Mapped[uuid.UUID | None] = mapped_column(
        _UUID, ForeignKey("message.id", ondelete="SET NULL")
    )
    processed_at: Mapped[datetime.datetime] = mapped_column(_TS, server_default=_NOW)
