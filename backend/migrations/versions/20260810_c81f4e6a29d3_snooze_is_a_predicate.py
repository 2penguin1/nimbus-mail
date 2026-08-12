"""snooze is a predicate

Block H. Drops `mailbox_message.is_snoozed`.

The column was a cached answer to a question the database can answer exactly:

    snoozed  <=>  snooze_until > now()

Nothing has to run for a snooze to expire. Time passes on its own. That is the whole of
block H, and it deletes the Redis sorted set, the Go worker, the one-second poll loop, the
per-shard lock and the leader election that HLD §9.5 specified — none of which were
solving a problem that `snooze_until > now()` has.

**Why drop the column rather than leave it unmaintained.** Two columns describing one
state can disagree, and `is_snoozed = true, snooze_until = NULL` was representable and
meant "snoozed forever, with no way out". Dropping the flag makes that state
unrepresentable. Leaving it in place unmaintained is the worse option: the next person to
write a query reaches for `is_snoozed`, and CLAUDE.md's rule is that a model which lies is
worse than no model at all.

**The read path does not get slower.** Measured on a 100k-message mailbox with 10%
snoozed: the stored flag and the computed predicate produce identical plans and identical
buffer counts (0.110 ms vs 0.103 ms, `Index Scan + Filter`, 4 buffers each). The flag
bought nothing and cost an entire subsystem.

**The downgrade loses nothing here, but it is lossy in principle.** It recomputes the flag
from `snooze_until`, so a row holding `is_snoozed = true` with `snooze_until = NULL` would
come back as `false`. Verified before applying: zero such rows exist, and nothing in the
codebase can write that combination.

Revision ID: c81f4e6a29d3
Revises: 9c3e5a1d7b42
Create Date: 2026-08-10

"""
from typing import Sequence, Union

from alembic import op

revision: str = "c81f4e6a29d3"
down_revision: Union[str, Sequence[str], None] = "9c3e5a1d7b42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("ALTER TABLE mailbox_message DROP COLUMN is_snoozed;")


def downgrade() -> None:
    op.execute("""
ALTER TABLE mailbox_message ADD COLUMN is_snoozed BOOLEAN NOT NULL DEFAULT false;
UPDATE mailbox_message SET is_snoozed = true WHERE snooze_until > now();
    """)
