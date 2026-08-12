"""The read path — docs/HLD.md §9.3, §10.2. Mailbox JWT.

Every query here filters on `visibility.readable_mailboxes()` and nothing else. That is
the whole of multi-tenant isolation on the read side: one endpoint that forgets it is a
cross-tenant leak.
"""

import datetime
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Response
from fastapi.responses import StreamingResponse
from pydantic import AwareDatetime, BaseModel
from sqlalchemy import exists, func, or_, select, tuple_, update
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db, storage
from nimbus.api import cursor as cursors
from nimbus.api import ranges, security, visibility
from nimbus.models import (
    BlobChunk,
    Chunk,
    MailboxMessage,
    Message,
    MessageAttachment,
)

router = APIRouter(prefix="/v1/messages", tags=["messages"])

FOLDERS = {"inbox", "archive", "trash", "spam"}


MAX_SNOOZE = datetime.timedelta(days=365)


class PatchRequest(BaseModel):
    is_read: bool | None = None
    folder: str | None = None
    # AwareDatetime, NOT datetime, and this is load-bearing. A client sending
    # "2027-01-01T09:00:00" with no zone would be interpreted in the SERVER's timezone —
    # measured on this machine (Asia/Kolkata), 09:00 became 03:30 UTC, so the mail would
    # reappear five and a half hours early. Silently: no exception, no log line. Rejecting
    # it at the boundary turns a wrong answer into a 422.
    snooze_until: AwareDatetime | None = None


def _snoozed(yes: bool):
    """Snoozed IS `snooze_until > now()`. There is no stored flag.

    Nothing runs to expire a snooze — the predicate simply stops being true, so accuracy
    is exact rather than bounded by a poll interval. `now()` is evaluated by Postgres at
    statement time, not by Python, so this cannot drift with a clock on an app server.
    """
    if yes:
        return MailboxMessage.snooze_until > func.now()
    return or_(
        MailboxMessage.snooze_until.is_(None),
        MailboxMessage.snooze_until <= func.now(),
    )


@router.get("")
async def list_messages(
    folder: str = Query("inbox"),
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = Query(None),
    snoozed: bool = Query(False, description="show the snoozed mail instead of hiding it"),
    mailbox_id: uuid.UUID = Depends(security.current_mailbox),
    session: AsyncSession = Depends(db.get_session),
):
    """One folder, newest first, cursor paginated.

    `?snoozed=true` inverts the snooze filter rather than adding a pseudo-folder, so
    `folder` stays an equality and `mailbox_message_list_idx` is still fully used. Without
    it a snoozed message would be unreachable except through search, and un-snoozing early
    would be impossible.
    """
    if folder not in FOLDERS:
        raise HTTPException(status_code=400, detail=f"unknown folder, pick one of {sorted(FOLDERS)}")

    readable = await visibility.readable_mailboxes(session, mailbox_id)

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
            MailboxMessage.snooze_until,
            MailboxMessage.mailbox_id,
            exists()
            .where(MessageAttachment.message_id == Message.id)
            .label("has_attachments"),
        )
        .join(MailboxMessage, MailboxMessage.message_id == Message.id)
        .where(
            MailboxMessage.mailbox_id.in_(readable),
            MailboxMessage.folder == folder,
            _snoozed(snoozed),
        )
        # Matches mailbox_message_list_idx (mailbox_id, folder, received_at DESC).
        .order_by(
            MailboxMessage.received_at.desc(),
            MailboxMessage.message_id.desc(),
            MailboxMessage.mailbox_id.desc(),
        )
        .limit(limit + 1)  # one extra row: is there another page?
    )
    if cursor:
        seen_at, seen_id, seen_mailbox = cursors.decode(cursor)
        # Postgres derives `received_at <= seen_at` from this row comparison and pushes
        # it into mailbox_message_list_idx as an index condition (verified with EXPLAIN),
        # so the cursor seeks rather than scanning every newer row.
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
                # Set and in the future = hidden from the default list. The UI needs it
                # to label a row reached via ?snoozed=true.
                "snooze_until": row.snooze_until,
                # Which inbox this copy is in — the caller's own, or a shared one they
                # are a member of. The UI needs it to show "via support@".
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


@router.get("/{message_id}")
async def get_message(
    message_id: uuid.UUID,
    copy: uuid.UUID | None = Query(None, alias="mailbox_id"),
    mailbox_id: uuid.UUID = Depends(security.current_mailbox),
    session: AsyncSession = Depends(db.get_session),
):
    """One message: what it says, plus the metadata of its attachments — not their bytes.

    The body is a stored column, not a live read of the raw `.eml`. Parsing that on
    every request costs 187 MB per 25 MB message (measured in block C) and would run
    inside an API request rather than a background worker. HLD §16 records the change.

    Takes `?mailbox_id=` for the same reason PATCH and DELETE do — a message id does not
    identify a row (§10.3). This used to be a bare `.first()` with no ORDER BY, so a
    reader holding two copies (their own address plus a shared mailbox) got whichever
    `is_read` and `folder` Postgres happened to return: the message would show as read
    or filed based on a coin flip. Now it names the copy or answers 409, like its
    siblings.
    """
    readable = await visibility.readable_mailboxes(session, mailbox_id)
    target = await _one_copy(session, message_id, readable, copy)
    row = (
        await session.execute(
            select(Message, MailboxMessage)
            .join(MailboxMessage, MailboxMessage.message_id == Message.id)
            .where(Message.id == message_id, MailboxMessage.mailbox_id == target)
        )
    ).first()
    if row is None:
        # 404, never 403. Telling someone a message exists but is not theirs is itself
        # a leak — it confirms an id they guessed is real.
        raise HTTPException(status_code=404, detail="No such message")
    message, state = row

    attachments = (
        await session.execute(
            select(
                MessageAttachment.blob_hash,
                MessageAttachment.filename,
                MessageAttachment.content_type,
                MessageAttachment.size_bytes,
            ).where(MessageAttachment.message_id == message_id)
        )
    ).all()

    return {
        "id": str(message.id),
        # WHICH copy this is. `_one_copy` already resolved it — returning it means a
        # caller that arrived without `?mailbox_id=` (from a thread, or a deep link) can
        # still act on the message afterwards. Without it the UI knows the message but
        # not the row, so PATCH and DELETE have nothing to name and the toolbar cannot
        # be drawn at all.
        "mailbox_id": str(target),
        "subject": message.subject,
        "from": message.from_addr,
        "sent_at": message.sent_at,
        "received_at": state.received_at,
        "size_bytes": message.size_bytes,
        "thread_id": str(message.thread_id) if message.thread_id else None,
        "message_id_header": message.message_id_header,
        "in_reply_to": message.in_reply_to,
        "is_read": state.is_read,
        "folder": state.folder,
        "snooze_until": state.snooze_until,
        "body_text": message.body_text,
        "body_html": message.body_html,
        "attachments": [
            {
                "blob_hash": a.blob_hash,
                "filename": a.filename,
                "content_type": a.content_type,
                "size_bytes": a.size_bytes,
                "url": f"/v1/messages/{message_id}/attachments/{a.blob_hash}",
            }
            for a in attachments
        ],
    }


async def _one_copy(
    session: AsyncSession,
    message_id: uuid.UUID,
    readable: set[uuid.UUID],
    chosen: uuid.UUID | None,
) -> uuid.UUID:
    """Which single copy of this message a write should touch.

    A message id does NOT identify a row. `mailbox_message`'s primary key is
    `(mailbox_id, message_id)`, and one email can legitimately reach one reader twice —
    CC'd to their own address and to a shared mailbox they belong to. Both rows are
    readable by them.

    Writing to every readable row is the dangerous default and it is what this replaces:
    marking a personal copy read would also mark the team's copy handled, and deleting a
    personal copy would destroy the team's only copy, irreversibly, with no confirmation
    and no undo.

    So: name the copy, or be told to. The list endpoint returns `mailbox_id` on every
    row precisely so a client can.
    """
    if chosen is not None:
        if chosen not in readable:
            raise HTTPException(status_code=404, detail="No such message")
        return chosen

    copies = (
        await session.scalars(
            select(MailboxMessage.mailbox_id).where(
                MailboxMessage.message_id == message_id,
                MailboxMessage.mailbox_id.in_(readable),
            )
        )
    ).all()
    if not copies:
        raise HTTPException(status_code=404, detail="No such message")
    if len(copies) > 1:
        raise HTTPException(
            status_code=409,
            detail=(
                f"this message is in {len(copies)} of your mailboxes; "
                "pass ?mailbox_id= to say which copy"
            ),
        )
    return copies[0]


@router.patch("/{message_id}")
async def patch_message(
    message_id: uuid.UUID,
    req: PatchRequest,
    copy: uuid.UUID | None = Query(None, alias="mailbox_id"),
    mailbox_id: uuid.UUID = Depends(security.current_mailbox),
    session: AsyncSession = Depends(db.get_session),
):
    """Read/unread, folders, and snooze. Acts on exactly one copy."""
    values = {}
    if req.is_read is not None:
        values["is_read"] = req.is_read
    if req.folder is not None:
        if req.folder not in FOLDERS:
            raise HTTPException(status_code=400, detail=f"unknown folder, pick one of {sorted(FOLDERS)}")
        values["folder"] = req.folder

    # `in model_fields_set`, NOT `is not None` — and only for this field. Un-snoozing is
    # `{"snooze_until": null}`, which `is not None` cannot tell apart from the field being
    # absent, so with the usual idiom un-snooze would be unexpressible. `is_read` and
    # `folder` keep `is not None` because null means nothing for either.
    if "snooze_until" in req.model_fields_set:
        if req.snooze_until is not None:
            now = datetime.datetime.now(datetime.timezone.utc)
            if req.snooze_until <= now:
                raise HTTPException(
                    status_code=400,
                    detail="snooze_until must be in the future; send null to un-snooze",
                )
            if req.snooze_until > now + MAX_SNOOZE:
                # A typo guard, not a system limit: `2226` for `2026` would otherwise
                # hide a message for two centuries with no way to notice.
                raise HTTPException(
                    status_code=400,
                    detail=f"snooze_until is more than {MAX_SNOOZE.days} days away",
                )
        values["snooze_until"] = req.snooze_until

    if not values:
        raise HTTPException(status_code=400, detail="nothing to change")

    readable = await visibility.readable_mailboxes(session, mailbox_id)
    target = await _one_copy(session, message_id, readable, copy)
    result = await session.execute(
        update(MailboxMessage)
        .where(
            MailboxMessage.message_id == message_id,
            MailboxMessage.mailbox_id == target,
        )
        .values(**values)
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="No such message")
    await session.commit()
    return {"id": str(message_id), "mailbox_id": str(target), **values}


@router.delete("/{message_id}", status_code=204)
async def delete_message(
    message_id: uuid.UUID,
    copy: uuid.UUID | None = Query(None, alias="mailbox_id"),
    mailbox_id: uuid.UUID = Depends(security.current_mailbox),
    session: AsyncSession = Depends(db.get_session),
):
    """Remove ONE copy. The bytes are not touched here.

    Deleting the `mailbox_message` row fires the trigger that drops `blob.refcount` and
    `mailbox.used_bytes`. The attachment's bytes survive as long as anyone else still
    points at them, and are collected later by GC (§9.7) only when nobody does.

    Exactly one copy, never "all the ones I can see". If this message is in both your
    own inbox and a shared mailbox you belong to, deleting without saying which is a
    409, not a guess — the wrong guess destroys a team's only copy of an email with no
    undo. See `_one_copy`.

    Deleting a SHARED mailbox's copy does remove it for every member. That is what a
    shared inbox means: one copy, one state, everyone sees the same thing. It is also
    why the copy has to be named explicitly rather than swept up by accident.
    """
    readable = await visibility.readable_mailboxes(session, mailbox_id)
    target = await _one_copy(session, message_id, readable, copy)
    result = await session.execute(
        MailboxMessage.__table__.delete().where(
            MailboxMessage.message_id == message_id,
            MailboxMessage.mailbox_id == target,
        )
    )
    if result.rowcount == 0:
        raise HTTPException(status_code=404, detail="No such message")
    await session.commit()
    return Response(status_code=204)


# --------------------------------------------------------------------------
# Attachment download  —  the streaming one
# --------------------------------------------------------------------------

@router.get("/{message_id}/attachments/{blob_hash}")
async def download_attachment(
    message_id: uuid.UUID,
    blob_hash: str,
    range_header: str | None = Header(None, alias="Range"),
    if_none_match: str | None = Header(None, alias="If-None-Match"),
    mailbox_id: uuid.UUID = Depends(security.current_mailbox),
    session: AsyncSession = Depends(db.get_session),
):
    """Stream an attachment, one chunk at a time, honouring `Range`.

    Memory is one chunk, never the whole file — a 40 MB attachment costs the same as a
    40 KB one. That is the entire reason the file was chunked on the way in.

    `Range` is what makes a download resumable and a video seekable: the client asks for
    the window it wants, we work out which chunks cover it, and we ask S3 for only those
    slices.
    """
    # A blob hash is a SHA-256 in hex and nothing else. Checked before it is bound into a
    # query because the path segment is raw user input: `.../attachments/%00` reaches
    # asyncpg, which cannot encode a NUL into TEXT and raises — a 500 on a URL anyone can
    # type. A value that is not 64 hex characters cannot match a row anyway, so 404 is
    # both the safe answer and the honest one.
    if len(blob_hash) != 64 or not all(c in "0123456789abcdef" for c in blob_hash.lower()):
        raise HTTPException(status_code=404, detail="No such attachment")

    readable = await visibility.readable_mailboxes(session, mailbox_id)

    # One query does authorisation and metadata together. The join to mailbox_message is
    # the authorisation: no readable row, no attachment.
    attachment = (
        await session.execute(
            select(
                MessageAttachment.filename,
                MessageAttachment.content_type,
                MessageAttachment.size_bytes,
                MessageAttachment.reseller_id,
            )
            .join(MailboxMessage, MailboxMessage.message_id == MessageAttachment.message_id)
            .where(
                MessageAttachment.message_id == message_id,
                MessageAttachment.blob_hash == blob_hash,
                MailboxMessage.mailbox_id.in_(readable),
            )
            .limit(1)
        )
    ).first()
    if attachment is None:
        raise HTTPException(status_code=404, detail="No such attachment")

    chunks = (
        await session.execute(
            select(Chunk.s3_key, Chunk.size_bytes)
            .join(
                BlobChunk,
                (BlobChunk.reseller_id == Chunk.reseller_id)
                & (BlobChunk.chunk_hash == Chunk.hash),
            )
            .where(
                BlobChunk.reseller_id == attachment.reseller_id,
                BlobChunk.blob_hash == blob_hash,
            )
            .order_by(BlobChunk.seq)  # seq IS the reassembly order
        )
    ).all()
    # A zero-byte attachment is legal MIME, and block C stores it as a blob with
    # chunk_count = 0 on purpose (worker/dedup.py). So "no chunks" is only an error when
    # the attachment claims to have bytes — otherwise this is an empty file and the right
    # answer is an empty 200, not a 404 on mail the user really did receive.
    if not chunks and attachment.size_bytes:
        raise HTTPException(status_code=404, detail="attachment has no stored chunks")

    total = sum(c.size_bytes for c in chunks)

    # The bytes at a blob hash are the bytes that hash to it — they can never change.
    # So the strongest possible validator is free, and a client re-opening a thread gets
    # a 304 instead of re-streaming the whole attachment out of S3.
    etag = f'"{blob_hash}"'
    if if_none_match and blob_hash in if_none_match:
        return Response(status_code=304, headers={"ETag": etag})

    try:
        window = ranges.parse(range_header, total)
    except ranges.Unsatisfiable:
        # 416 must carry the real length, or a client resuming a download it thinks is
        # longer has no way to correct itself. Accept-Ranges too: some clients read its
        # absence as "this server cannot resume" and give up rather than retrying.
        return Response(
            status_code=416,
            headers={"Content-Range": f"bytes */{total}", "Accept-Ranges": "bytes"},
        )

    start, end = window if window else (0, total - 1)
    plan = ranges.slice_chunks([c.size_bytes for c in chunks], start, end)
    keys = [chunks[index].s3_key for index, _, _ in plan]
    reads = [(key, offset, length) for key, (_, offset, length) in zip(keys, plan)]

    # Hand the connection back BEFORE streaming. FastAPI runs a yield-dependency's exit
    # code after the response is fully sent (default scope="request"), so without this
    # the pooled AsyncSession stays checked out for the entire transfer — a 40 MB
    # download on a slow phone holds a connection for minutes. `db.py` sets pool_size=10,
    # so eleven concurrent downloads would block EVERY other request, including /health
    # and login, until one finished. Everything the generator needs is already a plain
    # value; closing twice is a no-op.
    await session.close()

    async def body():
        for key, offset, length in reads:
            yield await storage.get_chunk(key, offset, length)

    # filename comes from the SENDER. Percent-encoding it (RFC 5987) means a filename
    # containing a quote or a newline cannot inject a header.
    name = attachment.filename or "attachment"
    headers = {
        "Content-Length": str(end - start + 1),
        "Accept-Ranges": "bytes",
        "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name)}",
        "ETag": etag,
        "Cache-Control": "private, max-age=31536000, immutable",
    }
    if window:
        headers["Content-Range"] = f"bytes {start}-{end}/{total}"

    return StreamingResponse(
        body(),
        status_code=206 if window else 200,
        media_type=attachment.content_type or "application/octet-stream",
        headers=headers,
    )
