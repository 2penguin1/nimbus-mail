"""Block G: the garbage collector. Reclaims the bytes nothing points at any more.

    uv run python -m nimbus.gc --dry-run     # count what would go, touch nothing
    uv run python -m nimbus.gc               # sweep

**Three phases, in this order, and the order is load-bearing.**

    1. `message` rows with no `mailbox_message` rows left   (releases message_attachment)
    2. `blob` rows at refcount 0, past the grace period      (releases blob_chunk)
    3. `chunk` rows at refcount 0                            (and their S3 objects)

Phase 1 must come first because `message_attachment` references `blob` with `NO ACTION`,
so deleting a `refcount = 0` blob while an attachment row still names it raises a foreign
key violation. Nothing else in the system deletes a `message` row — `DELETE /v1/messages`
removes one `mailbox_message` row — so without phase 1 every blob would reach refcount 0
and become **permanently uncollectable**, and the dedup engine would never free a byte.
HLD §9.7 has the proof.

Phase 2 must come before phase 3 for a reason that is easy to miss. `_store_attachment`
in `worker/pipeline.py` short-circuits on the BLOB row: if the blob exists it never looks
at chunks at all, never uploads, never notices anything missing. So a surviving blob whose
chunks had been collected would silently serve a corrupt attachment, and the chunk-level
dedup check would never get a chance to save us.

**A script, not a service.** One sweep then exit. A rerun is always safe, so a crash
needs no recovery — and a daemon would be a thing to supervise for a job that runs daily.

ponytail: nothing invokes this automatically. Ceiling: GC never runs until an operator or
a cron entry calls it, and the only symptom is a slowly growing storage bill. Upgrade
path: the cron entry belongs in the block K deploy checklist, where the schedule lives
next to everything else that has one.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import logging

from sqlalchemy import delete, exists, func, select, tuple_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db, storage
from nimbus.models import Blob, BlobChunk, Chunk, MailboxMessage, Message, MessageAttachment

log = logging.getLogger("nimbus.gc")

# How long a blob must have been garbage before its bytes go.
#
# This is NOT what makes the concurrent-upload race safe — the foreign key is, and it
# holds at a grace period of zero (see migration 9c3e5a1d7b42). What the wait actually
# buys is a window in which a human can notice that a refcount reached zero when it
# should not have, before the bytes are unrecoverable. A default argument rather than a
# setting, so tests pass `grace=timedelta(0)` without an environment variable.
GRACE = datetime.timedelta(hours=24)

# Rows per phase per transaction. Small enough that phase 3 holds its row locks across
# only one S3 round trip, and that a DeleteObjects request stays well under the 1000-key
# cap. One transaction for the whole sweep would hold locks for minutes and block every
# delivery touching a shared chunk; one per row would be a round trip per row.
BATCH = 500


async def sweep(session: AsyncSession, grace: datetime.timedelta = GRACE,
                dry_run: bool = False) -> dict[str, int]:
    """Run all three phases. Returns what each one removed (or would remove).

    **A dry run does the real deletes and rolls the whole sweep back at the end.** It has
    to, and the earlier version — which selected candidates per phase and rolled back
    immediately — reported `0 blobs, 0 chunks` in essentially every real garbage state.

    The reason is the phase ordering the sweep is built on. Phase 2 only considers a blob
    once no `message_attachment` row names it, and the ONLY thing that removes those rows
    is phase 1's cascade. Undo phase 1 and phase 2 sees nothing, so phase 3 sees nothing
    either. Two of the three numbers were structurally always zero — on a command whose
    entire job is telling an operator whether GC is worth running. Nothing schedules GC
    automatically, so "the dry run said 0" is exactly how a storage bill grows forever.

    The cost is that a preview holds its row locks for the whole sweep instead of one
    batch, and a `mailbox_message` insert needs `FOR KEY SHARE` on rows phase 1 has locked
    — so a dry run against a large backlog can block delivery while it runs. That is the
    right trade for a command an operator types deliberately, and it is the reason this
    is not how the real sweep works: that one commits per batch precisely so it does not.
    """
    try:
        messages = await _phase1_orphan_messages(session, dry_run)
        blobs = await _phase2_blobs(session, grace, dry_run)
        chunks, s3_failures = await _phase3_chunks(session, dry_run)
    finally:
        if dry_run:
            # Nothing was committed by any phase, so one rollback undoes the preview.
            # In `finally` because a phase raising midway must not leave the deletes
            # sitting in an open transaction holding locks.
            await session.rollback()

    return {
        "messages": messages,
        "blobs": blobs,
        "chunks": chunks,
        # Not a count of anything deleted — see `main()`. It exists so a failure to
        # delete S3 objects can change the exit code instead of only writing a log line.
        "s3_failures": s3_failures,
    }


# --------------------------------------------------------------------------
# Phase 1 — messages nobody has a copy of
# --------------------------------------------------------------------------

def _orphan() -> object:
    """`message` rows with no `mailbox_message` row left pointing at them."""
    return ~exists().where(MailboxMessage.message_id == Message.id)


async def _phase1_orphan_messages(session: AsyncSession, dry_run: bool) -> int:
    removed = 0
    while True:
        # Two statements, not one, and this is the whole guard.
        #
        # A single `DELETE ... WHERE NOT EXISTS (child)` is NOT safe. If a concurrent
        # transaction is inserting a `mailbox_message` row, the DELETE blocks on the
        # foreign key's lock — and when it unblocks it deletes the parent ANYWAY.
        # Postgres re-evaluates quals against the row itself (EvalPlanQual), and this row
        # was never updated, only key-share locked, so the NOT EXISTS keeps its stale
        # snapshot. The cascade then removes the copy that had just been committed: mail
        # silently gone, and the trigger drops blob.refcount and used_bytes with it.
        #
        # FOR UPDATE here, then a SEPARATE delete which under READ COMMITTED takes a
        # fresh snapshot that includes anything committed while we waited.
        ids = (
            await session.scalars(
                select(Message.id)
                .where(_orphan())
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not ids:
            break

        result = await session.execute(
            delete(Message).where(Message.id.in_(ids), _orphan())
        )
        # A dry run deletes but never commits; `sweep()` rolls the lot back at the end.
        # The loop still terminates, because the uncommitted delete is visible to this
        # transaction's own next SELECT.
        if not dry_run:
            await session.commit()
        removed += result.rowcount
        # No `break` when rowcount is 0, and — unlike phase 2 — none is needed.
        #
        # The DELETE really can remove fewer rows than we locked. FOR UPDATE conflicts
        # with the FOR KEY SHARE a `mailbox_message` insert takes on its parent, so no
        # child row can appear while we hold the lock (verified with two live sessions:
        # the insert blocks). One window survives that: an insert that COMMITTED after
        # this SELECT's snapshot but before the scan reached the row. The snapshot says
        # orphan, the lock is free, the row is returned — and the DELETE's fresh snapshot
        # sees the child and refuses it. That is what the repeated `_orphan()` is for.
        #
        # It cannot spin. The next SELECT's snapshot is strictly newer than this DELETE's,
        # so every row the DELETE just refused is still non-orphan and is not selected
        # again — `ids` shrinks on every pass with no help from anyone. Phase 2 needs its
        # `break` because its failure path rolls the batch back UNCHANGED, so the same
        # rows would be selected and fail forever; phase 1 has no such path.
    return removed


# --------------------------------------------------------------------------
# Phase 2 — attachments nobody reaches
# --------------------------------------------------------------------------

async def _phase2_blobs(session: AsyncSession, grace: datetime.timedelta,
                        dry_run: bool) -> int:
    # The cutoff is computed by POSTGRES, not by Python. `refcount_zeroed_at` is written
    # by the refcount trigger using the database's `now()`, so comparing it against the
    # application host's clock measures the two machines' skew as well as the grace
    # period. At 24 hours that is noise, but `grace` exists to be lowered — tests pass
    # `timedelta(0)` — and at zero a database clock a few seconds ahead of the app makes
    # phase 2 silently collect nothing at all. One clock, read at one place.
    removed = 0
    while True:
        candidates = (
            await session.execute(
                select(Blob.reseller_id, Blob.hash)
                .where(
                    Blob.refcount == 0,
                    # The clock is refcount_zeroed_at, NOT created_at. created_at is when
                    # the bytes were first stored and never moves, so an old blob whose
                    # last reference vanished a second ago would pass instantly.
                    Blob.refcount_zeroed_at < func.now() - grace,
                    # Belt and braces against phase 1 not having run: deleting a blob an
                    # attachment row still names is a foreign key violation.
                    ~exists().where(
                        (MessageAttachment.reseller_id == Blob.reseller_id)
                        & (MessageAttachment.blob_hash == Blob.hash)
                    ),
                )
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not candidates:
            break

        keys = [(reseller_id, blob_hash) for reseller_id, blob_hash in candidates]

        # Hand back the chunk references these blobs hold, BEFORE deleting them —
        # blob_chunk cascades away with the blob and then there is nothing left to count.
        #
        # count(*), not 1. One blob can list the same chunk twice: 8 MB of zeros is two
        # identical 4 MB slices, and block C incremented the chunk once per blob_chunk
        # row. Decrementing by one would strand that chunk at refcount 1 for ever — its
        # bytes never reclaimed, and no error anywhere.
        held = (
            select(
                BlobChunk.reseller_id,
                BlobChunk.chunk_hash,
                func.count().label("n"),
            )
            .where(tuple_(BlobChunk.reseller_id, BlobChunk.blob_hash).in_(keys))
            .group_by(BlobChunk.reseller_id, BlobChunk.chunk_hash)
            .subquery()
        )
        await session.execute(
            update(Chunk)
            .where(Chunk.reseller_id == held.c.reseller_id, Chunk.hash == held.c.chunk_hash)
            .values(refcount=Chunk.refcount - held.c.n)
        )

        try:
            result = await session.execute(
                delete(Blob).where(
                    tuple_(Blob.reseller_id, Blob.hash).in_(keys), Blob.refcount == 0
                )
            )
        except IntegrityError:
            # A message stored but not yet delivered can slip past the NOT EXISTS above —
            # it is a subquery and gets no re-check. Roll the batch back, which undoes the
            # chunk decrements with it, and STOP this phase.
            #
            # `break`, not `continue`: the same batch would be selected again and fail
            # again, forever. The cost of stopping is that one contended blob caps how
            # much this sweep reclaims — the rest goes on the next run, which is the right
            # trade against a loop that never ends.
            await session.rollback()
            log.exception("phase 2: batch failed a foreign key check, stopping this phase")
            break

        # We hold FOR UPDATE on every candidate, so every one must have deleted. Fewer
        # means the decrement above ran for a blob that survived — chunk refcounts now
        # too low, and phase 3 would free bytes somebody is still reading. Abort loudly:
        # this is the one place in the system where continuing is worse than crashing.
        if result.rowcount != len(keys):
            await session.rollback()
            raise RuntimeError(
                f"phase 2: locked {len(keys)} blobs but deleted {result.rowcount} — "
                "chunk refcounts may have been decremented for a surviving blob"
            )

        if not dry_run:
            await session.commit()
        removed += result.rowcount
    return removed


# --------------------------------------------------------------------------
# Phase 3 — chunks, and the only place S3 objects are deleted
# --------------------------------------------------------------------------

async def _phase3_chunks(session: AsyncSession, dry_run: bool) -> tuple[int, int]:
    """Returns (chunks removed, S3 keys the store refused to delete)."""
    removed = 0
    s3_failures = 0
    while True:
        candidates = (
            await session.execute(
                select(Chunk.reseller_id, Chunk.hash)
                .where(Chunk.refcount == 0)
                .limit(BATCH)
                .with_for_update(skip_locked=True)
            )
        ).all()
        if not candidates:
            break

        keys = [(reseller_id, chunk_hash) for reseller_id, chunk_hash in candidates]

        # No grace period here, deliberately. A chunk only reaches zero when the last blob
        # referencing it was deleted, and that blob already served the full wait. A second
        # one would double the delay and protect nothing.
        #
        # The S3 keys come from RETURNING, never from the candidate list — only the rows
        # this DELETE actually removed may have their objects deleted.
        deleted_keys = (
            await session.scalars(
                delete(Chunk)
                .where(tuple_(Chunk.reseller_id, Chunk.hash).in_(keys), Chunk.refcount == 0)
                .returning(Chunk.s3_key)
            )
        ).all()
        if not deleted_keys:
            # Not `rollback()` unconditionally: in a dry run that would throw away phases
            # 1 and 2 as well, and the preview would report their work and then undo it
            # early. `sweep()` owns the rollback for a dry run.
            if not dry_run:
                await session.rollback()
            break

        # Objects deleted INSIDE the transaction, before the commit. This is the ordering
        # that matters and it is not the obvious one.
        #
        # Committing first and then calling S3 leaves a window: the worker's next
        # transaction finds no chunk row, re-uploads the bytes, and our DeleteObjects
        # lands afterwards — a live chunk at refcount 1 pointing at nothing. Content
        # addressing does not help, because the re-upload is byte-identical and the delete
        # still arrives last. Silent, and only discovered when someone downloads.
        #
        # Doing it here, a worker either sees the row (skips the upload, then blocks on
        # our row lock and fails loudly on the foreign key when we commit — Kafka retries
        # and the retry re-uploads) or does not see it (and uploads after our delete has
        # already happened). Both are correct.
        #
        # ponytail: ANY rollback after this call — a crash, a failed commit, a dropped
        # connection, a statement timeout — leaves the rows alive at refcount 0 with their
        # objects already gone. Ceiling: that state persists until the NEXT SWEEP, not for
        # milliseconds, and it does NOT heal on its own. A delivery of those exact bytes
        # arriving first finds the chunk row in `already_stored`, skips the upload, and
        # raises the refcount to 1 — a live chunk pointing at nothing, found only when
        # someone downloads. (The next sweep does clear it if no such delivery arrives,
        # because S3 ignores a missing key; that is recovery, not self-healing.)
        # Upgrade path: versioning on the chunk bucket with noncurrent-version expiry
        # makes every delete undoable. Infrastructure, not code — block K checklist.
        # A dry run stops here: everything above is undone by sweep()'s rollback, but an
        # S3 delete is not undoable, so the one thing a preview must never do is call it.
        failed = [] if dry_run else await storage.delete_chunks(list(deleted_keys))
        if failed:
            # Rows gone, objects still there: storage we are billed for with no row naming
            # it. It is tempting to roll back instead — do not. Rolling back keeps every
            # chunk row alive at refcount 0 while the keys that DID delete are already
            # gone, which is the corrupt state the ponytail note above describes: the next
            # delivery of those bytes finds the row, skips the upload, and raises the
            # refcount on a chunk pointing at nothing. Committing costs money; rolling
            # back costs correctness. Commit, and make the failure loud enough to act on —
            # `main()` turns this into a non-zero exit so a cron job cannot report success.
            log.error("phase 3: S3 refused %d key(s), now orphaned: %s", len(failed), failed)
            s3_failures += len(failed)

        if not dry_run:
            await session.commit()
        removed += len(deleted_keys)
    return removed, s3_failures


async def main() -> None:
    parser = argparse.ArgumentParser(description="Nimbus garbage collection sweep")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="report exactly what a real sweep would remove, then roll all of it back",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )

    async with db.Session() as session:
        counts = await sweep(session, dry_run=args.dry_run)
    await db.close()

    verb = "would remove" if args.dry_run else "removed"
    log.info(
        "%s %d orphan message(s), %d blob(s), %d chunk(s)",
        verb, counts["messages"], counts["blobs"], counts["chunks"],
    )
    if args.dry_run:
        log.info("dry run: everything above was rolled back, and no S3 object was touched.")

    if counts["s3_failures"]:
        # Exit non-zero, because the rows are already gone and only this process knows
        # which objects were left behind. A cron job that reports success here means the
        # log line is the sole record of storage nobody is tracking any more.
        log.error(
            "%d S3 object(s) could not be deleted and are now unreferenced — see the "
            "keys logged above", counts["s3_failures"],
        )
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())
