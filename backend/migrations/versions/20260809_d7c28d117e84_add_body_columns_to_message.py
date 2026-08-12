"""add body columns to message

The read path (block E) has to return what an email SAYS, and until now nothing stored
it — `message` held headers and `raw_s3_key` only, so the body of every mail in the
system was unreachable through the API.

The alternative was reading it live from the raw `.eml` on every request. Measured, that
parse costs **187 MB peak for a 25 MB message** (block C, tracemalloc): the stdlib MIME
parser materialises the whole tree including attachments, and there is no streaming
decode. Two or three concurrent readers would exhaust the shared `t3.small` in HLD §14 —
on the endpoint every opened email hits.

Storing it at delivery costs a few KB per row and nothing at read time. The worker
already parses the full tree, so extracting the body there is free. Block F needs the
same text anyway to build `message_index.tsv`, so this is work we owed regardless.

Nullable, because both are genuinely optional: a message may be text-only, HTML-only, or
(for a pure attachment carrier) neither.

Revision ID: d7c28d117e84
Revises: 2b2703f2e926
Create Date: 2026-08-09

"""
from typing import Sequence, Union

from alembic import op

revision: str = "d7c28d117e84"
down_revision: Union[str, Sequence[str], None] = "2b2703f2e926"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
ALTER TABLE message
    ADD COLUMN body_text TEXT,
    ADD COLUMN body_html TEXT;
    """)


def downgrade() -> None:
    op.execute("""
ALTER TABLE message
    DROP COLUMN IF EXISTS body_html,
    DROP COLUMN IF EXISTS body_text;
    """)
