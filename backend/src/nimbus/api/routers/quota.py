"""Block G's read side — docs/HLD.md §10.2, §11. Mailbox JWT.

One endpoint, three numbers, and the gap between two of them is the whole product:

    logical  — what the user believes they used     (40 copies x 25 MB = 1 GB)
    physical — what we actually stored              (25 MB)

HLD §11 decides we bill LOGICAL. The saving is our margin, not a discount, and physical
is what proves the storage engine works. Showing both side by side is the demo.

**This is the one read endpoint that does NOT call `visibility.readable_mailboxes()`,
and that is correct.** Every other read returns messages, which can live in a shared
mailbox the caller is a member of. This returns two columns off the caller's own
`mailbox` row, identified by the token. Quota is a per-mailbox-row fact — each mailbox
has its own `quota_bytes` — so summing it over everything a reader can see would be
adding up numbers that each belong to a different limit. A reviewer will flag the absent
filter; this paragraph is the answer.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db
from nimbus.api import security
from nimbus.models import Blob, Mailbox, MailboxMessage, MessageAttachment

router = APIRouter(prefix="/v1/quota", tags=["quota"])


@router.get("")
async def get_quota(
    mailbox_id: uuid.UUID = Depends(security.current_mailbox),
    session: AsyncSession = Depends(db.get_session),
):
    """What this mailbox thinks it used, what it really costs, and its limit.

    `physical_bytes` is the sum of the DISTINCT blobs this mailbox's messages reach. Two
    other definitions were possible and both are worse:

    - Summing `size_bytes` per attachment row double-counts a file attached to twenty
      messages, which is the number `logical_bytes` already reports.
    - `size_bytes / refcount` — each mailbox's amortised share — adds up correctly across
      a reseller, but it is meaningless to the person reading it: "you are storing 0.6 MB
      of a 25 MB file" is not a sentence anyone acts on.

    **The number chosen must never be summed across mailboxes.** Three mailboxes sharing
    one 25 MB attachment each report 25 MB; the reseller total is 25 MB, not 75. The
    reseller-level figure is its own query (`SUM(size_bytes)` over that reseller's blobs)
    and answers a different question.

    Two ways this figure flatters us, both worth knowing before it goes on a dashboard:
    `logical_bytes` counts the whole raw message including base64 expansion (~1.37x the
    binary size), while `physical_bytes` counts only the decoded attachment bytes — so
    part of the apparent saving is encoding overhead rather than dedup. And it excludes
    the raw `.eml` archive entirely, which is stored per message and never deduplicated.
    """
    row = (
        await session.execute(
            select(Mailbox.used_bytes, Mailbox.quota_bytes).where(Mailbox.id == mailbox_id)
        )
    ).first()
    if row is None:
        # The token survived its mailbox being deleted. `security.current_mailbox`
        # already checks this, so reaching here means it was deleted mid-request.
        raise HTTPException(status_code=404, detail="No such mailbox")

    # ponytail: recomputed on every call rather than maintained as a counter. Ceiling:
    # one index scan of this mailbox's copies plus a probe per distinct attachment;
    # measured 56-70 ms on a 100k-message mailbox (the 9 ms this shows on a small one is
    # not the ceiling — an earlier version of this comment quoted it as if it were).
    # Fine for a dashboard, but there is no cache and no rate limit, so a logged-in
    # mailbox can repeat it.
    # Upgrade path: cache in Redis with a short TTL. Do NOT maintain a per-mailbox physical
    # counter instead — a second copy of a derived number is a second thing to drift, and
    # `used_bytes` already shows what that costs.
    # DISTINCT on the full blob key `(reseller_id, hash)`, never on the hash alone —
    # dedup is scoped per reseller (HLD §12), so the hash by itself does not name a blob.
    reachable = (
        select(MessageAttachment.reseller_id, MessageAttachment.blob_hash)
        .join(MailboxMessage, MailboxMessage.message_id == MessageAttachment.message_id)
        .where(MailboxMessage.mailbox_id == mailbox_id)
        .distinct()
        .subquery()
    )
    physical = await session.scalar(
        select(func.coalesce(func.sum(Blob.size_bytes), 0))
        .select_from(Blob)
        .join(
            reachable,
            (Blob.reseller_id == reachable.c.reseller_id)
            & (Blob.hash == reachable.c.blob_hash),
        )
    )

    return {
        "logical_bytes": row.used_bytes,
        "physical_bytes": physical,
        "quota_bytes": row.quota_bytes,
    }
