"""search index infrastructure

Block F. The `message_index` table, its GIN index and its foreign keys were all created
by the initial migration, but nothing had ever written a row to it. Wiring up the write
path exposed two things that were wrong about the table as built.

**1. The GIN index was on `tsv` alone, so the tenant filter could not use it.**

Every search filters on `mailbox_id` first — that is `visibility.readable_mailboxes()`,
and it is the whole of multi-tenant isolation on the read side. With the index covering
only `tsv`, Postgres scans the posting list for a word across EVERY tenant in the system
and then rechecks the mailbox afterwards. Searching for a common word would do work
proportional to all mail everywhere, which is precisely the "one giant global index"
HLD §9.4 rules out.

A multicolumn GIN index fixes it, and unlike btree it can be used with any subset of its
columns — so `GIN (mailbox_id, tsv)` serves a text search, a metadata-only search, and
the combination equally well. It needs `btree_gin` to supply a GIN operator class for
`uuid`; that is a standard contrib module, confirmed present as version 1.3 in the
`postgres:16-alpine` image this project runs.

**2. Nothing deleted an index row when one copy of a message was deleted.**

`message_index` referenced `message` and `mailbox`, but not `mailbox_message` — and
`DELETE /v1/messages/{id}` (block E) deletes exactly one `mailbox_message` row. Its index
row would have survived, so a "deleted" message would still be findable by search, and
opening the result would 404. Worse in a shared mailbox: deleting your own copy would
leave the shared copy's row untouched but yours orphaned and still matching.

The fix is a composite foreign key onto `mailbox_message`'s primary key, which is exactly
the `(mailbox_id, message_id)` pair `message_index` already uses as its own. The database
then maintains this with no application code, which is the only version that cannot be
forgotten by a future endpoint.

That makes the two original foreign keys redundant — any row reaching `message_index` is
now guaranteed to reference a live delivery, which is itself already tied to a live
`message` and `mailbox`. Dropping them leaves one relationship to reason about instead of
three overlapping ones, and turns "an index row implies a real delivery" into something
Postgres enforces rather than something the write path has to remember forever.

`message_index_msg_idx` existed only to make the old `message -> message_index` cascade
fast. That cascade now runs `message -> mailbox_message` (which has its own index) and
then to `message_index` by its primary key, so the index is dead weight and goes too.

Revision ID: 4f1a9c2b8e07
Revises: d7c28d117e84
Create Date: 2026-08-10

"""
from typing import Sequence, Union

from alembic import op

revision: str = "4f1a9c2b8e07"
down_revision: Union[str, Sequence[str], None] = "d7c28d117e84"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # btree_gin gives GIN an operator class for uuid, which is what allows mailbox_id to
    # share an index with a tsvector. Without it the CREATE INDEX below fails outright.
    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gin;")

    op.execute("""
DROP INDEX IF EXISTS message_index_tsv_idx;
DROP INDEX IF EXISTS message_index_msg_idx;

CREATE INDEX message_index_tsv_mailbox_idx
    ON message_index USING gin (mailbox_id, tsv);

ALTER TABLE message_index DROP CONSTRAINT IF EXISTS message_index_message_id_fkey;
ALTER TABLE message_index DROP CONSTRAINT IF EXISTS message_index_mailbox_id_fkey;

ALTER TABLE message_index
    ADD CONSTRAINT message_index_mailbox_message_fkey
    FOREIGN KEY (mailbox_id, message_id)
    REFERENCES mailbox_message (mailbox_id, message_id)
    ON DELETE CASCADE;
    """)


def downgrade() -> None:
    # btree_gin is deliberately NOT dropped, for the same reason pgcrypto never is: an
    # extension is shared, and dropping one because a single index stopped needing it
    # would break anything else that started depending on it.
    op.execute("""
ALTER TABLE message_index DROP CONSTRAINT IF EXISTS message_index_mailbox_message_fkey;

ALTER TABLE message_index
    ADD CONSTRAINT message_index_message_id_fkey
    FOREIGN KEY (message_id) REFERENCES message (id) ON DELETE CASCADE;
ALTER TABLE message_index
    ADD CONSTRAINT message_index_mailbox_id_fkey
    FOREIGN KEY (mailbox_id) REFERENCES mailbox (id) ON DELETE CASCADE;

DROP INDEX IF EXISTS message_index_tsv_mailbox_idx;
CREATE INDEX message_index_tsv_idx ON message_index USING gin (tsv);
CREATE INDEX message_index_msg_idx ON message_index (message_id);
    """)
