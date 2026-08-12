"""Block F's read side — docs/HLD.md §9.4, ARCHITECTURE.md diagram 16. Mailbox JWT.

One endpoint. It takes what the user typed, turns it into filters (`search_query.py`),
and compiles those into one query against the `GIN (mailbox_id, tsv)` index.

**The mailbox ids come from `visibility.readable_mailboxes()`, never from the query
string and never from the JWT alone.** A shared mailbox holds ONE copy that its members
read (HLD §9.2), so filtering on the token's own mailbox id would make every shared
mailbox's mail permanently unsearchable — invisible to the members, and invisible to the
mailbox itself, which has `password_hash = NULL` and cannot be logged into. It would
still be listable through `GET /v1/messages`, so the two read surfaces would silently
disagree. Diagram 16 used to say `= $1` and was corrected for exactly this.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import exists, func, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db
from nimbus.api import cursor as cursors
from nimbus.api import search_query, security, visibility
from nimbus.models import MailboxMessage, Message, MessageAttachment, MessageIndex
from nimbus.search_index import REGCONFIG

router = APIRouter(prefix="/v1/search", tags=["search"])


@router.get("")
async def search(
    # No max_length. FastAPI would answer 422 for a long paste, which contradicts the
    # whole point of a parser that never raises — and it made the parser's own truncation
    # unreachable over HTTP, so two guards disagreed and one was dead. `parse()` truncates
    # to MAX_QUERY_CHARS, so nothing longer than that ever reaches Postgres.
    q: str = Query(...),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    mailbox_id: uuid.UUID = Depends(security.current_mailbox),
    session: AsyncSession = Depends(db.get_session),
):
    """Search everything this reader can see. Newest match first, cursor paginated.

    **Newest first, not most relevant.** `ts_rank` has to score every candidate row and
    sort on the result, which is not index-backed and — the reason that settles it —
    cannot be keyset-paginated: there is no stable ordering to compare a cursor against,
    so paging would fall back to `OFFSET` and reintroduce the skipped-row bug §10.4 bans
    for the inbox. In one person's mailbox recency is most of what relevance means
    anyway. If ranking is ever wanted it is a separate `sort=` mode, not a change here.

    **Every folder is searched, including trash, and so is snoozed mail.** Both differ
    from `GET /v1/messages`, deliberately: the inbox shows what needs attention now,
    search is "find that email". Someone who snoozed a message until Monday and then goes
    looking for it expects to find it. Every row carries its `folder` so the UI can label
    a trashed or snoozed hit rather than hide it.
    """
    parsed = search_query.parse(q)
    if parsed.is_empty:
        raise HTTPException(status_code=400, detail="empty query")

    readable = await visibility.readable_mailboxes(session, mailbox_id)

    has_attachments = exists().where(MessageAttachment.message_id == Message.id)

    query = (
        select(
            Message.id,
            Message.subject,
            Message.from_addr,
            Message.size_bytes,
            Message.thread_id,
            MailboxMessage.is_read,
            MailboxMessage.folder,
            MailboxMessage.received_at,
            MailboxMessage.mailbox_id,
            has_attachments.label("has_attachments"),
        )
        .select_from(MessageIndex)
        # Joined on BOTH key columns. On message_id alone this would be a cartesian
        # product for anyone who can read two copies of one message (their own plus a
        # shared mailbox's) — every such result would appear twice. Diagram 16 had this
        # bug drawn into it.
        .join(
            MailboxMessage,
            (MailboxMessage.mailbox_id == MessageIndex.mailbox_id)
            & (MailboxMessage.message_id == MessageIndex.message_id),
        )
        .join(Message, Message.id == MessageIndex.message_id)
        # Filtered on MessageIndex.mailbox_id, not MailboxMessage.mailbox_id. Same rows
        # either way, but only this form lets Postgres satisfy the tenant filter inside
        # the GIN (mailbox_id, tsv) scan instead of rechecking it after the join.
        .where(MessageIndex.mailbox_id.in_(readable))
        # ponytail: a full sort of the match set — no index can serve it, since the GIN
        # index is already spent on the WHERE. Measured at 100k messages in one mailbox:
        # 5 ms for a selective word, 73-76 ms for a query matching most of the mailbox
        # (`before:2099-01-01`), the sort spilling to disk. Inside HLD §13's 100 ms,
        # without much room. Ceiling: one mailbox around 100k messages. Upgrade path: an
        # index on `(mailbox_id, received_at DESC)` — deliberately not added now, because
        # `mailbox_message_list_idx` already covers the inbox and a second index on the
        # same table costs every delivery a write.
        .order_by(
            MailboxMessage.received_at.desc(),
            MailboxMessage.message_id.desc(),
            MailboxMessage.mailbox_id.desc(),
        )
        .limit(limit + 1)  # one extra row: is there another page?
    )

    if parsed.free_text:
        # websearch_to_tsquery, NOT to_tsquery. to_tsquery raises a syntax error on
        # ordinary typed input — `foo &` is enough — which would turn a user's typo into
        # a 500. websearch_to_tsquery never raises AND understands "exact phrase",
        # -exclude and or, which is why the parser leaves quotes alone for it.
        query = query.where(
            MessageIndex.tsv.bool_op("@@")(
                func.websearch_to_tsquery(REGCONFIG, parsed.free_text)
            )
        )
    if parsed.from_contains:
        # Sender-supplied text, matched as a substring. `ilike` escapes nothing, so a
        # query containing % or _ is a wildcard — harmless here (it only ever widens the
        # user's OWN result set, which is already scoped to what they may read).
        query = query.where(Message.from_addr.ilike(f"%{parsed.from_contains}%"))
    if parsed.has_attachment:
        query = query.where(has_attachments)
    if parsed.before:
        # received_at, not message.sent_at. sent_at is the sender's own Date: header —
        # attacker-supplied, and NULL whenever it is missing or malformed, so filtering
        # on it would silently exclude those messages from every date search.
        query = query.where(
            MailboxMessage.received_at < search_query.as_utc(parsed.before)
        )
    if parsed.after:
        query = query.where(
            MailboxMessage.received_at >= search_query.as_utc(parsed.after)
        )

    if cursor:
        seen_at, seen_id, seen_mailbox = cursors.decode(cursor)
        query = query.where(
            tuple_(
                MailboxMessage.received_at,
                MailboxMessage.message_id,
                MailboxMessage.mailbox_id,
            )
            < (seen_at, seen_id, seen_mailbox)
        )

    rows = (await session.execute(query)).all()
    has_more = len(rows) > limit
    rows = rows[:limit]

    return {
        "query": {
            "text": parsed.free_text,
            "from": parsed.from_contains,
            "has_attachment": parsed.has_attachment,
            "before": parsed.before,
            "after": parsed.after,
        },
        "messages": [
            {
                "id": str(row.id),
                "subject": row.subject,
                "from": row.from_addr,
                "received_at": row.received_at,
                "is_read": row.is_read,
                "folder": row.folder,
                "size_bytes": row.size_bytes,
                "thread_id": str(row.thread_id) if row.thread_id else None,
                "has_attachments": row.has_attachments,
                "mailbox_id": str(row.mailbox_id),
            }
            for row in rows
        ],
        "next_cursor": (
            cursors.encode(rows[-1].received_at, rows[-1].id, rows[-1].mailbox_id)
            if has_more and rows
            else None
        ),
    }
