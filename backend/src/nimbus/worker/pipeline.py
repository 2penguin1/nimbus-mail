"""Blocks C and D: turn one raw email into stored bytes and delivered inboxes.

Everything here runs inside ONE Postgres transaction, opened by the caller and committed
only if every step succeeds. The Kafka offset is acked after that commit, never before.

Three blocks, one transaction:

    block C   MIME split, chunk, hash, dedup, store       -> blob / chunk / message rows
    block D   route each recipient, fan out                -> mailbox_message rows
    block F   build the searchable text                    -> message_index rows

C and D run together because a message stored but not delivered is invisible to everyone,
and a message delivered but not stored is a dangling row. Either both land or neither does.

F rides along because it needs the parsed body, which only exists here — but it is NOT
all-or-nothing with the other two. Its write sits in a savepoint: a message that cannot
be indexed is still delivered, because unsearchable mail is recoverable and lost mail is
not. See `search_index.write`.

Which counter moves where:

    chunk.refcount   counts blob_chunk rows      -> block C, in _store_new_blob
    blob.refcount    counts mailbox_message rows -> block D, in routing.deliver
    mailbox.used_bytes                           -> block D, in routing.deliver

Nothing in the database maintains `chunk.refcount` — the trigger on `mailbox_message`
only moves `blob.refcount` and `mailbox.used_bytes`. If this code does not increment it,
nothing ever will and GC would treat every chunk as garbage.
"""

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus.models import (
    Blob,
    BlobChunk,
    Chunk,
    Domain,
    Message,
    MessageAttachment,
    ProcessedEvent,
)
from nimbus import search_index, storage
from nimbus.worker import dedup, mime, routing

log = logging.getLogger("nimbus.worker")


async def process_event(session: AsyncSession, event: dict) -> bool:
    """Handle one `mail.received` event. Returns False if it was already done.

    The caller commits. Raising anything here rolls the whole thing back, including
    every reseller's rows — we would rather redo one message than half-store it.
    """
    s3_key = event["s3_key"]

    # ---- 1. The replay guard, FIRST, before any work at all. -------------------------
    #
    # Kafka is at-least-once: it WILL hand us the same event twice. Without this, a
    # redelivery would insert a second `message` row and re-run every refcount
    # increment — silent duplication, not a crash.
    #
    # ON CONFLICT DO NOTHING rather than catching the unique violation, because an
    # exception aborts the whole transaction we are about to do the real work in.
    # If another worker holds this key uncommitted, Postgres makes us wait here until
    # it commits or rolls back, so we never both proceed.
    claimed = await session.scalar(
        pg_insert(ProcessedEvent)
        .values(event_key=s3_key)
        .on_conflict_do_nothing(index_elements=[ProcessedEvent.event_key])
        .returning(ProcessedEvent.event_key)
    )
    if claimed is None:
        log.info("already processed, skipping: %s", s3_key)
        return False

    # ---- 2. Which customer does this mail belong to? ---------------------------------
    #
    # Needed before anything else, because reseller_id is half of every blob and chunk
    # primary key — we cannot even name a blob without it.
    groups = await _group_by_reseller(session, event["recipients"])
    if not groups:
        # Every recipient's domain vanished between RCPT TO and now. HLD §9.2 step 5:
        # log and drop. There is nobody left to bounce to — the SMTP connection closed
        # minutes ago, and outbound mail is an explicit non-goal.
        log.warning("no surviving recipient domains, dropping: %s", s3_key)
        return True

    # ---- 3. Open the envelope, ONCE, off the event loop. -----------------------------
    #
    # Content is identical for every reseller, so parsing per group would burn CPU for
    # nothing. Only the storage rows differ.
    headers, (body_text, body_html), attachments = await asyncio.to_thread(
        _read_and_split, s3_key
    )

    received_at = datetime.fromisoformat(event["received_at"])

    # Every reseller's message row first, so the attachment loop below can hold one
    # attachment's chunks at a time instead of every attachment's at once.
    message_ids = {}
    for reseller_id, recipients in groups.items():
        message_ids[reseller_id] = await _store_message(
            session, reseller_id, event, headers, received_at, body_text, body_html
        )
        log.info(
            "stored message=%s reseller=%s recipients=%d attachments=%d",
            message_ids[reseller_id], reseller_id, len(recipients), len(attachments),
        )

    # Attachment outer, reseller inner. The other way round would keep every
    # attachment's chunks alive for the whole function; this way each one's bytes are
    # released before the next is hashed. Hashing is CPU-bound, so it goes to a thread
    # for the same reason the parse did.
    for attachment in attachments:
        size_bytes = len(attachment.data)
        blob_hash, chunks = await asyncio.to_thread(dedup.chunk_and_hash, attachment.data)
        attachment.data = b""  # the chunks hold these bytes now; drop the second copy
        for reseller_id, message_id in message_ids.items():
            await _store_attachment(
                session, reseller_id, message_id, attachment, size_bytes, blob_hash, chunks
            )
        del chunks

    # ---- 4. What the message says, ready for search (block F) ------------------------
    #
    # Built once, outside the loop: the searchable words are a property of the email, not
    # of who received it or which reseller they belong to. Everything it needs is already
    # in hand from step 3, so this costs no extra parse and no second read from S3.
    search_text = search_index.build(
        headers.subject,
        headers.from_addr,
        [a.filename for a in attachments],
        body_text,
        body_html,
    )

    # ---- 5. Route, fan out (block D), then index what was delivered (block F) --------
    #
    # AFTER the attachments, not before: deliver() raises blob.refcount for every blob
    # this message carries, and it finds them through message_attachment. Running it
    # first would count nothing and leave every blob at refcount 0 — invisible until GC
    # deleted attachments that inboxes were still pointing at.
    for reseller_id, recipients in groups.items():
        mailboxes = await routing.resolve(session, reseller_id, recipients)
        delivered = await routing.deliver(
            session,
            reseller_id,
            message_ids[reseller_id],
            mailboxes,
            event["size_bytes"],
            received_at,
        )
        # Index exactly what was delivered, not what we tried to deliver. On a replay
        # `delivered` is empty, so nothing is written twice — the same guard serves both
        # the refcounts and the index, which is why deliver() returns the set.
        indexed = await search_index.write(
            session, message_ids[reseller_id], delivered, search_text
        )
        log.info(
            "delivered message=%s to %d mailbox(es) from %d recipient(s), indexed %d",
            message_ids[reseller_id], len(delivered), len(recipients), indexed,
        )

    # processed_event.message_id is a single column, so it can only name one message.
    # With recipients at two different resellers there are two, and it stays NULL. The
    # replay guard is the PRIMARY KEY on event_key and does not depend on this column —
    # see docs/HLD.md §9.1b.
    if len(message_ids) == 1:
        await session.execute(
            update(ProcessedEvent)
            .where(ProcessedEvent.event_key == s3_key)
            .values(message_id=next(iter(message_ids.values())))
        )
    return True


def _read_and_split(s3_key: str):
    """BLOCKING — always call via asyncio.to_thread. Download, parse, extract.

    The three slowest things in the block happen here, and none of them release the GIL
    for long enough to matter on the event loop: the download, the MIME parse, and the
    base64 decode. Measured at 596 ms for one 25 MB attachment with no network at all.

    The parsed message tree never leaves this function. That matters for memory: the
    tree holds every part's still-encoded payload, ~34 MB for a 25 MB attachment, and
    returning it would keep all of that alive for the whole transaction. Here it becomes
    garbage the moment we return — which is also why the body has to be pulled out
    here rather than by whoever wants it later.
    """
    raw = storage.open_raw_message(s3_key)
    message = mime.parse(raw)
    # The body comes out here too, while the tree is already in hand. Re-parsing it
    # on every read would cost 187 MB per 25 MB message, inside an API request.
    return mime.headers(message), mime.body(message), mime.attachments(message)


async def _group_by_reseller(session: AsyncSession, recipients: list[str]) -> dict:
    """Bucket recipient addresses by the reseller that owns their domain.

    Domain ownership is the ONLY lookup needed here, and that is deliberate. Aliases,
    catch-alls and shared mailboxes all resolve to targets under the same domain, so
    none of them can change which reseller a message belongs to. Doing the full routing
    chain here would drag block D's logic into block C.
    """
    domains = {addr.rsplit("@", 1)[-1] for addr in recipients if "@" in addr}
    owners = dict(
        (
            await session.execute(
                select(Domain.name, Domain.reseller_id).where(Domain.name.in_(domains))
            )
        ).all()
    )

    groups = defaultdict(list)
    for address in recipients:
        reseller_id = owners.get(address.rsplit("@", 1)[-1])
        if reseller_id is None:
            log.warning("recipient's domain no longer exists, dropping: %s", address)
            continue
        groups[reseller_id].append(address)
    return groups


async def _store_message(
    session, reseller_id, event, headers, received_at, body_text, body_html
) -> uuid.UUID:
    message = Message(
        reseller_id=reseller_id,
        raw_s3_key=event["s3_key"],
        message_id_header=headers.message_id,
        in_reply_to=headers.in_reply_to,
        subject=headers.subject,
        from_addr=headers.from_addr,
        body_text=body_text,
        body_html=body_html,
        sent_at=headers.sent_at,
        # From the event, not the column default. Kafka lag would otherwise stamp
        # messages with the time we got round to them instead of the time they arrived,
        # and inboxes sort on this.
        received_at=received_at,
        size_bytes=event["size_bytes"],
        thread_id=await _thread_id(session, reseller_id, headers.in_reply_to),
    )
    session.add(message)
    await session.flush()  # assigns message.id without committing
    return message.id


async def _thread_id(session, reseller_id, in_reply_to) -> uuid.UUID:
    """Find the conversation this reply belongs to, or start a new one.

    Filtered by reseller_id, which is not optional. `Message-ID` headers are written by
    the SENDER — anyone can put any value there. Without the tenant filter, an attacker
    could forge a Message-ID matching another customer's mail and have their message
    threaded into that customer's conversation. That is a cross-tenant metadata leak,
    the worst class of bug in this system (HLD §10.1).
    """
    if in_reply_to:
        parent = await session.scalar(
            select(Message.thread_id)
            .where(
                Message.reseller_id == reseller_id,
                Message.message_id_header == in_reply_to,
            )
            # message_id_header is NOT unique — senders can and do reuse a Message-ID.
            # Without an order the parent picked is whichever row Postgres happens to
            # return, so the same reply could thread differently on a replay. Oldest
            # wins: the first message to claim an id is the real parent.
            .order_by(Message.received_at)
            .limit(1)
        )
        if parent is not None:
            return parent
    return uuid.uuid4()


async def _store_attachment(
    session, reseller_id, message_id, attachment, size_bytes, blob_hash, chunks
) -> None:
    """Store one attachment's bytes — or discover we already have them.

    This function is where the entire business case lives. The CEO's 25 MB deck sent to
    40 colleagues at one reseller reaches here 1 time with a miss and 39 with a hit; the
    39 upload nothing and store nothing but a ~40 byte row.
    """
    known = await session.scalar(
        select(Blob.hash).where(Blob.reseller_id == reseller_id, Blob.hash == blob_hash)
    )

    if known is None:
        await _store_new_blob(session, reseller_id, blob_hash, chunks, size_bytes)

    # The link from this message to those bytes. The filename lives here and not on the
    # blob: identical bytes may be `invoice.pdf` from one sender and `Q3-final.pdf` from
    # another. The primary key is (message_id, blob_hash), so the same file attached
    # twice to one email collapses to a single row — correct, since it is one blob.
    await session.execute(
        pg_insert(MessageAttachment)
        .values(
            message_id=message_id,
            blob_hash=blob_hash,
            reseller_id=reseller_id,
            filename=attachment.filename,
            content_type=attachment.content_type,
            size_bytes=size_bytes,
        )
        .on_conflict_do_nothing(
            index_elements=[MessageAttachment.message_id, MessageAttachment.blob_hash]
        )
    )


async def _store_new_blob(session, reseller_id, blob_hash, chunks, size_bytes) -> None:
    """Upload the bytes we do not already hold, then write the rows that describe them.

    ORDER: S3 first, Postgres second, commit last. Fail towards keeping data — the same
    direction the receiver chose in HLD §9.1a. A crash after uploading leaves the object
    at a content-addressed key, which is exactly where the retry writes, so the retry
    overwrites it with identical bytes. Nothing is orphaned and nothing needs sweeping.

    The uploads happen BEFORE any row is inserted so that no database row lock is held
    while we wait on the network. Inserting first would make every concurrent worker
    touching the same chunk queue behind a 4 MB HTTP request.
    """
    hashes = [h for h, _ in chunks]
    already_stored = set(
        (
            await session.scalars(
                select(Chunk.hash).where(
                    Chunk.reseller_id == reseller_id, Chunk.hash.in_(hashes)
                )
            )
        ).all()
    )

    # Upload everything first, THEN insert. Interleaving them means holding a row lock
    # on the first chunk across every remaining 4 MB HTTP request — a second worker
    # storing a blob that shares that chunk blocks for the whole upload run, not just
    # for one chunk. Separating the loops keeps the locks to the database round trips.
    missing = []
    for chunk_hash, data in chunks:
        if chunk_hash not in already_stored:
            # Recorded before the upload, not after, because one blob can contain the
            # same chunk twice — 40 MB of zeros is ten identical 4 MB slices. Without
            # this the set never grows and we PUT the same object ten times for one
            # stored copy: ten times the upload bytes and requests, all discarded.
            already_stored.add(chunk_hash)
            missing.append((chunk_hash, data))
            await storage.put_chunk(dedup.chunk_s3_key(reseller_id, chunk_hash), data)

    for chunk_hash, data in missing:
        await session.execute(
            pg_insert(Chunk)
            .values(
                reseller_id=reseller_id,
                hash=chunk_hash,
                size_bytes=len(data),
                s3_key=dedup.chunk_s3_key(reseller_id, chunk_hash),
            )
            .on_conflict_do_nothing(index_elements=[Chunk.reseller_id, Chunk.hash])
        )

    await session.execute(
        pg_insert(Blob)
        .values(
            reseller_id=reseller_id,
            hash=blob_hash,
            size_bytes=size_bytes,
            chunk_count=len(chunks),
            # Counts mailbox_message rows, and block D creates those. Zero is right here.
            refcount=0,
        )
        .on_conflict_do_nothing(index_elements=[Blob.reseller_id, Blob.hash])
    )

    for seq, (chunk_hash, _) in enumerate(chunks):
        created = await session.scalar(
            pg_insert(BlobChunk)
            .values(
                reseller_id=reseller_id,
                blob_hash=blob_hash,
                seq=seq,
                chunk_hash=chunk_hash,
            )
            .on_conflict_do_nothing(
                index_elements=[BlobChunk.reseller_id, BlobChunk.blob_hash, BlobChunk.seq]
            )
            .returning(BlobChunk.seq)
        )
        # `is not None`, NOT a truth test: seq 0 is a real row and is falsy. Getting this
        # wrong would skip the first chunk's refcount on every single blob, and GC would
        # then delete chunk 0 of every attachment in the system while it was still in use.
        if created is not None:
            await session.execute(
                update(Chunk)
                .where(Chunk.reseller_id == reseller_id, Chunk.hash == chunk_hash)
                .values(refcount=Chunk.refcount + 1)
            )
