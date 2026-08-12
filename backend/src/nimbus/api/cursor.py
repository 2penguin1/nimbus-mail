"""Keyset pagination cursors. Pure — encode, decode, nothing else.

Keyset, not `OFFSET`. An inbox receives mail while it is being paged through, and
`OFFSET 50` re-counts from the top every time — so a message arriving between page 1 and
page 2 pushes one row down across the boundary and the reader never sees it. A cursor
points at a row, so new mail at the top cannot shift what comes after it.

**The key is `(received_at, message_id, mailbox_id)`, and all three are needed.**
`received_at` alone is not unique: a fan-out writes many rows in one transaction, all
with the same timestamp. `(received_at, message_id)` is not unique either: one message
delivered to your own mailbox AND to a shared mailbox you can read is two rows carrying
the same message id at the same instant. A cursor that stopped at `message_id` would
strictly-skip the second row whenever a page boundary fell between them, and that copy
would never appear again on any page.

It lives here rather than in a router because block E's inbox and block F's search page
through the same rows in the same order. Two copies of this would be two chances to get
the comparison subtly different, and the symptom — a row that silently never appears —
is invisible in testing and permanent in production.
"""

import base64
import binascii
import datetime
import uuid

from fastapi import HTTPException

Key = tuple[datetime.datetime, uuid.UUID, uuid.UUID]


def encode(
    received_at: datetime.datetime, message_id: uuid.UUID, mailbox_id: uuid.UUID
) -> str:
    raw = f"{received_at.isoformat()}|{message_id}|{mailbox_id}".encode()
    return base64.urlsafe_b64encode(raw).decode()


def decode(cursor: str) -> Key:
    """Read a cursor back. A malformed one is a 400, not a 500.

    The cursor is ours and opaque — clients only ever echo back what we handed them — so
    anything unreadable is a client bug or someone poking at the API, never a server
    fault.
    """
    try:
        timestamp, message_id, mailbox_id = (
            base64.urlsafe_b64decode(cursor.encode()).decode().split("|")
        )
        return (
            datetime.datetime.fromisoformat(timestamp),
            uuid.UUID(message_id),
            uuid.UUID(mailbox_id),
        )
    except (ValueError, binascii.Error, UnicodeDecodeError):
        raise HTTPException(status_code=400, detail="invalid cursor")
