"""Block L1. Turn on domain ownership verification without dropping live mail.

`domain.verified` has existed since the initial migration and nothing has ever written
or read it, so every row in every database is `false`. Block L1 makes `api/addresses.py`
filter the Redis address set on that column — which means the moment the new code runs,
every existing address disappears from `valid_addresses` and the receiver starts
answering 550 to ALL mail. Silently. Nothing logs an error, because nothing is broken.

This migration is the only thing standing between the feature and that outage. It
grandfathers what already exists: anything provisioned before verification was a concept
is treated as verified, because there was no way for its owner to have proved anything.

Verification applies from here on. New domains start `false` (the column default) and
have to pass `POST /v1/domains/{id}/verify`.

No schema change — the column, its type and its default are all already correct. This is
a data migration and nothing else.

Revision ID: e5b71c04d9a3
Revises: c81f4e6a29d3
"""

from alembic import op

revision = "e5b71c04d9a3"
down_revision = "c81f4e6a29d3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Unconditional, not `WHERE created_at < now()`. Any row that exists when this runs
    # predates enforcement by definition, and a time-based predicate would race with an
    # order committing during the migration.
    op.execute("UPDATE domain SET verified = true")


def downgrade() -> None:
    # Deliberately NOT `UPDATE domain SET verified = false`.
    #
    # Downgrading means going back to code that ignores the column. Clearing it there
    # changes nothing — but if the same database is then upgraded again, every domain
    # that had genuinely passed the DNS check would be un-verified and would stop
    # receiving mail until someone re-ran verification for each one by hand.
    #
    # Leaving the data alone is the reversible choice. A `verified` flag that the old
    # code does not read is inert, which is exactly what it was before this block.
    pass
