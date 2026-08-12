"""index message_id_header for threading

Block C resolves a reply's conversation by looking up its `In-Reply-To` value against
`message.message_id_header`. That runs once for every reply we deliver, and without an
index it is a sequential scan of the whole message table — at the 100k-message target in
HLD §13 that is the slowest query in the system, on the hot delivery path.

The index leads with `reseller_id` because the lookup is always tenant-scoped. That
filter is not a performance detail: `Message-ID` is written by the SENDER, so anyone can
forge one. Without the tenant filter an attacker could thread their message into another
customer's conversation, which is a cross-tenant leak (HLD §10.1).

Partial, because a message with no `Message-ID` header can never be the parent of
anything — indexing those rows would only make the index bigger for no lookup.

Revision ID: 2b2703f2e926
Revises: 0a7973bf2530
Create Date: 2026-08-09 19:39:58.841479

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2b2703f2e926'
down_revision: Union[str, Sequence[str], None] = '0a7973bf2530'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("""
CREATE INDEX message_thread_lookup_idx
    ON message (reseller_id, message_id_header)
    WHERE message_id_header IS NOT NULL;
    """)


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS message_thread_lookup_idx;")
