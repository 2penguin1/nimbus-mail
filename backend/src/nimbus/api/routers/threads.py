"""Conversation view — docs/HLD.md §10.2. Mailbox JWT.

A thread is every message sharing a `thread_id`, oldest first, because a conversation
reads top to bottom. That is the opposite of the inbox, which is newest first.

`thread_id` is set at delivery: a reply's `In-Reply-To` header is matched against its
parent's `Message-ID` within the same reseller, and it inherits the parent's thread.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db
from nimbus.api import security, visibility
from nimbus.models import MailboxMessage, Message

router = APIRouter(prefix="/v1/threads", tags=["messages"])

MAX_THREAD_MESSAGES = 500


@router.get("/{thread_id}")
async def get_thread(
    thread_id: uuid.UUID,
    mailbox_id: uuid.UUID = Depends(security.current_mailbox),
    session: AsyncSession = Depends(db.get_session),
):
    """Every message in this conversation that the caller may read.

    Filtered by readable mailboxes like everything else, so a thread shows only the
    parts of the conversation that actually reached this user. A message they were not
    a recipient of stays invisible even though it shares the thread — the alternative
    leaks the existence and subject of mail they were never sent.
    """
    readable = await visibility.readable_mailboxes(session, mailbox_id)
    rows = (
        await session.execute(
            select(
                Message.id,
                Message.subject,
                Message.from_addr,
                Message.sent_at,
                Message.size_bytes,
                MailboxMessage.is_read,
                MailboxMessage.folder,
                MailboxMessage.received_at,
            )
            .join(MailboxMessage, MailboxMessage.message_id == Message.id)
            .where(Message.thread_id == thread_id, MailboxMessage.mailbox_id.in_(readable))
            .order_by(MailboxMessage.received_at, Message.id)  # oldest first, stable
            # The only bound on this endpoint. A mailing-list thread can run to
            # thousands of messages, and every other list in the API caps out at 200 —
            # an unbounded one serialises the lot into a single JSON response.
            .limit(MAX_THREAD_MESSAGES)
        )
    ).all()

    # One message reaching this reader twice — their own address and a shared mailbox
    # they belong to — is two rows carrying the same message. A conversation should
    # show it once. `mailbox_message`'s key is (mailbox_id, message_id), so de-duplicate
    # on the message.
    seen: set[uuid.UUID] = set()
    rows = [row for row in rows if not (row.id in seen or seen.add(row.id))]

    if not rows:
        raise HTTPException(status_code=404, detail="No such thread")

    return {
        "thread_id": str(thread_id),
        "messages": [
            {
                "id": str(row.id),
                "subject": row.subject,
                "from": row.from_addr,
                "sent_at": row.sent_at,
                "received_at": row.received_at,
                "size_bytes": row.size_bytes,
                "is_read": row.is_read,
                "folder": row.folder,
            }
            for row in rows
        ],
    }
