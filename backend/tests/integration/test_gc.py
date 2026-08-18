"""Block G: garbage collection and quota, end to end through the real stack.

Needs Docker, the API, the SMTP receiver AND the worker.

**The first test is the one that matters.** Everything else here checks that GC frees
what it should; that one checks it does not free what it must not. Deleting live data is
the worst thing this system can do — worse than never collecting anything, because the
failure is silent and unrecoverable, and the user only finds out when they open a
three-month-old email and the attachment is gone.
"""

import asyncio
import datetime
import secrets
import smtplib
import time
from email.message import EmailMessage

import httpx
import pytest
from sqlalchemy import delete, func, select

from nimbus import db, gc, storage
from nimbus.api import security
from nimbus.config import settings
from nimbus.models import (
    Blob,
    BlobChunk,
    Chunk,
    Domain,
    Mailbox,
    MailboxMessage,
    Message,
    MessageAttachment,
    ProcessedEvent,
    Reseller,
)

pytestmark = pytest.mark.integration

API = "http://127.0.0.1:8000"
TAG = secrets.token_hex(4)
DOMAIN = f"gc-{TAG}.example"
NO_GRACE = datetime.timedelta(0)

# 8 MB of zeros: TWO identical 4 MB chunks in one blob. That makes blob_chunk hold the
# same chunk_hash twice, so chunk.refcount is 2 for one blob — the case where GC must
# decrement by count(*) and not by 1. Decrementing by 1 strands the chunk at refcount 1
# for ever, its bytes never reclaimed, and nothing anywhere raises.
PAYLOAD = b"\x00" * (8 * 1024 * 1024)


def send(to_addrs: list[str], subject: str) -> None:
    msg = EmailMessage()
    msg["From"] = "sender@globex.com"
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content("see attached")
    msg.add_attachment(PAYLOAD, maintype="application", subtype="pdf", filename="big.pdf")
    with smtplib.SMTP("127.0.0.1", 2525, timeout=120) as s:
        s.send_message(msg, from_addr="sender@globex.com", to_addrs=to_addrs)


async def wait_for(session, reseller_id, want: int, seconds: int = 120) -> int:
    deadline = time.monotonic() + seconds
    got = 0
    while time.monotonic() < deadline:
        session.expire_all()
        got = await session.scalar(
            select(func.count())
            .select_from(MailboxMessage)
            .join(Message, Message.id == MailboxMessage.message_id)
            .where(Message.reseller_id == reseller_id)
        )
        if got >= want:
            return got
        await asyncio.sleep(2)
    return got


async def _counts(session, reseller_id) -> tuple[int, int, int]:
    session.expire_all()
    return (
        await session.scalar(
            select(func.count()).select_from(Message).where(Message.reseller_id == reseller_id)
        ),
        await session.scalar(
            select(func.count()).select_from(Blob).where(Blob.reseller_id == reseller_id)
        ),
        await session.scalar(
            select(func.count()).select_from(Chunk).where(Chunk.reseller_id == reseller_id)
        ),
    )


async def test_gc_and_quota(verify_domain) -> None:
    async with db.Session() as session:
        key, key_hash = security.new_api_key()
        session.add(Reseller(name=f"gc-{TAG}", api_key_hash=key_hash))
        await session.commit()
        reseller_id = await session.scalar(
            select(Reseller.id).where(Reseller.name == f"gc-{TAG}")
        )

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                order = await client.post(
                    f"{API}/v1/orders",
                    json={"domain": DOMAIN, "mailboxes": ["alice", "bob"]},
                    headers={"Authorization": f"Bearer {key}", "Idempotency-Key": TAG},
                )
                assert order.status_code == 201, order.text

                # Block L1 (HLD §9.6a): a `.example` domain has no DNS zone, so it can never pass
                # the real TXT check — and unverified means its addresses never reach Redis and
                # every send below would get 550. See tests/integration/conftest.py.
                await verify_domain(session, DOMAIN)
                passwords = {
                    m["local_part"]: m["temp_password"] for m in order.json()["mailboxes"]
                }

                # ONE email to BOTH — one message, one blob, two copies, refcount 2.
                send([f"alice@{DOMAIN}", f"bob@{DOMAIN}"], "shared deck")
                assert await wait_for(session, reseller_id, want=2) == 2

                blob = (
                    await session.execute(
                        select(Blob.hash, Blob.refcount, Blob.size_bytes, Blob.chunk_count)
                        .where(Blob.reseller_id == reseller_id)
                    )
                ).first()
                assert blob.refcount == 2, f"refcount {blob.refcount}, expected 2"
                assert blob.chunk_count == 2, "8 MB should be two 4 MB chunks"
                chunk_refs = (
                    await session.execute(
                        select(Chunk.hash, Chunk.refcount).where(Chunk.reseller_id == reseller_id)
                    )
                ).all()
                assert len(chunk_refs) == 1, "two IDENTICAL chunks must dedup to one row"
                assert chunk_refs[0].refcount == 2, (
                    f"chunk refcount {chunk_refs[0].refcount} — one blob references this "
                    "chunk twice, so block C must have counted it twice"
                )
                print("ok   1 blob, 1 chunk row referenced twice, refcount 2")

                # ---- quota ------------------------------------------------------
                async def token_for(local: str) -> str:
                    r = await client.post(
                        f"{API}/v1/auth/login",
                        json={"address": f"{local}@{DOMAIN}", "password": passwords[local]},
                    )
                    assert r.status_code == 200, r.text
                    return r.json()["token"]

                alice = {"Authorization": f"Bearer {await token_for('alice')}"}
                bob = {"Authorization": f"Bearer {await token_for('bob')}"}

                started = time.monotonic()
                r = await client.get(f"{API}/v1/quota", headers=alice)
                elapsed_ms = (time.monotonic() - started) * 1000
                assert r.status_code == 200, r.text
                quota = r.json()
                assert set(quota) == {"logical_bytes", "physical_bytes", "quota_bytes"}
                assert quota["physical_bytes"] == blob.size_bytes, (
                    f"physical {quota['physical_bytes']} != blob {blob.size_bytes}"
                )
                assert quota["logical_bytes"] > 0
                print(f"ok   quota: logical {quota['logical_bytes']:,} vs physical "
                      f"{quota['physical_bytes']:,} bytes, in {elapsed_ms:.0f} ms")

                # Both mailboxes report the SAME physical bytes for the same shared blob.
                # That is correct and it is why the number must never be summed: the
                # reseller stored 8 MB once, not 16 MB.
                r = await client.get(f"{API}/v1/quota", headers=bob)
                assert r.json()["physical_bytes"] == quota["physical_bytes"]
                print("ok   both mailboxes report the same shared bytes (never sum them)")

                # ---- 1. THE TEST THAT MATTERS: a sweep must not touch live data ----
                message_id = await session.scalar(
                    select(Message.id).where(Message.reseller_id == reseller_id)
                )
                r = await client.delete(f"{API}/v1/messages/{message_id}", headers=alice)
                assert r.status_code == 204, r.text

                session.expire_all()
                assert await session.scalar(
                    select(Blob.refcount).where(Blob.reseller_id == reseller_id)
                ) == 1, "deleting one copy must leave the other"

                # The sweep is GLOBAL — it also collects whatever earlier runs left
                # behind, which is exactly its job. So assert on THIS reseller's rows,
                # not on the returned totals: "GC left my data alone" is the property,
                # "GC did nothing at all" would be a different and much weaker test.
                mine_before = await _counts(session, reseller_id)
                await gc.sweep(session, grace=NO_GRACE)
                mine_after = await _counts(session, reseller_id)
                assert mine_after == mine_before, (
                    f"GC changed this reseller's rows {mine_before} -> {mine_after} while "
                    "bob still holds a copy — this is the failure that loses mail"
                )
                r = await client.get(f"{API}/v1/messages/{message_id}", headers=bob)
                assert r.status_code == 200, "bob's copy did not survive the sweep"
                attachment = r.json()["attachments"][0]
                got = await client.get(f"{API}{attachment['url']}", headers=bob)
                assert got.status_code == 200
                assert got.content == PAYLOAD, "the bytes came back CORRUPT after a sweep"
                print("ok   sweep left the live copy untouched and byte-identical")

                # ---- 2. the grace clock -------------------------------------------
                # The blob is old by created_at (it was stored minutes ago, but so was
                # everything). What must protect it now is refcount_zeroed_at, which is
                # only set once the LAST copy goes.
                r = await client.delete(f"{API}/v1/messages/{message_id}", headers=bob)
                assert r.status_code == 204, r.text
                session.expire_all()
                zeroed_at, refcount = (
                    await session.execute(
                        select(Blob.refcount_zeroed_at, Blob.refcount)
                        .where(Blob.reseller_id == reseller_id)
                    )
                ).first()
                assert refcount == 0
                assert zeroed_at is not None, (
                    "the trigger did not stamp refcount_zeroed_at — the grace period has "
                    "no clock and GC would collect this blob immediately"
                )

                await gc.sweep(session, grace=datetime.timedelta(hours=24))
                session.expire_all()
                assert await session.scalar(
                    select(func.count()).select_from(Blob).where(Blob.reseller_id == reseller_id)
                ) == 1, (
                    "a blob that became garbage seconds ago was collected under a "
                    "24-hour grace period — the clock is reading created_at again"
                )
                print("ok   grace period measured from refcount_zeroed_at, not created_at")

                # ---- 3. the full sweep actually frees things ----------------------
                before = await _counts(session, reseller_id)
                # (0, 1, 1) and not (1, 1, 1): the grace period applies to phase 2 only.
                # The previous sweep already took the orphan `message` row — a message
                # nobody holds a copy of is garbage immediately, and holding it back would
                # only delay the blob behind it. The BYTES are what the grace period
                # protects, and they are still here.
                assert before == (0, 1, 1), f"expected no message, 1 blob, 1 chunk; got {before}"
                # Read the S3 key BEFORE the sweep. Phase 3 deletes the row and the object
                # in one transaction, so afterwards there is nothing left to ask where the
                # bytes were.
                s3_key = await session.scalar(
                    select(Chunk.s3_key).where(Chunk.reseller_id == reseller_id)
                )

                # ---- 3a. the dry run must predict the real sweep ------------------
                # This is the ONLY path an operator runs before trusting GC, and nothing
                # exercised it until now. The earlier implementation rolled back after
                # each phase's SELECT, so phase 2 never saw the message_attachment rows
                # phase 1 would have released and phase 3 never saw the chunks phase 2
                # would have zeroed: it reported `0 blobs, 0 chunks` in every real garbage
                # state. An operator reading that concludes the dedup engine has nothing
                # to reclaim and never schedules the sweep.
                #
                # Asserting equality with the real sweep below is what makes this test
                # meaningful — a dry run that predicts nothing is worse than no dry run,
                # because it looks like a measurement.
                preview = await gc.sweep(session, grace=NO_GRACE, dry_run=True)
                assert await _counts(session, reseller_id) == before, (
                    "a dry run changed the database"
                )
                assert storage.client.list_objects_v2(
                    Bucket=settings.s3_chunk_bucket, Prefix=s3_key
                ).get("Contents"), "a dry run deleted an S3 object — nothing undoes that"

                real = await gc.sweep(session, grace=NO_GRACE)
                assert (preview["blobs"], preview["chunks"]) == (real["blobs"], real["chunks"]), (
                    f"dry run predicted {preview}, the real sweep did {real} — the "
                    "preview is not a preview"
                )
                assert preview["blobs"] > 0, (
                    "dry run reported 0 blobs on a database that had garbage to collect"
                )
                print(f"ok   dry run predicted the real sweep exactly: "
                      f"{preview['blobs']} blob(s), {preview['chunks']} chunk(s)")

                after = await _counts(session, reseller_id)
                # The chunk reaching 0 is also the count(*) proof: one blob referenced it
                # twice, so a decrement of 1 would have stranded it at refcount 1 for ever.
                assert after == (0, 0, 0), f"{before} -> {after}, expected everything gone"
                print(f"ok   full sweep: messages/blobs/chunks {before} -> {after}")

                # The ROW going away is not the point. The BYTES are — reclaiming them is
                # the entire reason GC exists, and every count above is blind to whether
                # it happened. `storage.delete_chunks` treats a key that is not there as a
                # SUCCESS (deliberately: that is what makes a rerun safe), so if phase 3
                # ever handed S3 the wrong strings — a different RETURNING column, a stale
                # candidate list — it would report no failures, the counts would still
                # read (0, 0, 0), and the objects would be billed for ever with no row
                # naming them. Measured on this machine before this assertion existed:
                # 223 objects in the chunk bucket against 8 chunk rows.
                assert not storage.client.list_objects_v2(
                    Bucket=settings.s3_chunk_bucket, Prefix=s3_key
                ).get("Contents"), (
                    f"chunk row is gone but its S3 object survived: {s3_key} — GC freed "
                    "a database row and reclaimed no bytes at all"
                )
                print(f"ok   the S3 object is gone too, not just the row: {s3_key}")

                # ---- 4. no negative refcounts anywhere ----------------------------
                for table, name in ((Blob, "blob"), (Chunk, "chunk")):
                    negative = await session.scalar(
                        select(func.count()).select_from(table).where(table.refcount < 0)
                    )
                    assert negative == 0, f"{negative} {name} row(s) went negative"
                print("ok   no refcount went negative — no double-decrement")

                # ---- 5. idempotent ------------------------------------------------
                # By now this reseller has nothing left and the pre-existing garbage was
                # collected by the first sweep, so a second sweep must be a total no-op.
                again = await gc.sweep(session, grace=NO_GRACE)
                assert again == {
                    "messages": 0, "blobs": 0, "chunks": 0,
                    # Zero here is a real assertion, not padding: a non-zero count means
                    # S3 refused a delete, the rows went anyway, and those objects are now
                    # billed with nothing naming them. `main()` exits 1 on it.
                    "s3_failures": 0,
                }, again
                print("ok   a second sweep removes nothing and raises nothing")

                # ---- 6. quota after everything is gone ----------------------------
                r = await client.get(f"{API}/v1/quota", headers=alice)
                assert r.status_code == 200
                assert r.json()["physical_bytes"] == 0
                assert r.json()["logical_bytes"] == 0, (
                    f"used_bytes is {r.json()['logical_bytes']} after every message was "
                    "deleted — the trigger and deliver() have drifted apart"
                )
                print("ok   quota returns to zero once the mail is gone")

                # ---- 7. used_bytes reconciles across every mailbox ----------------
                drift = (
                    await session.execute(
                        select(Mailbox.id, Mailbox.used_bytes)
                        .join(Domain, Domain.id == Mailbox.domain_id)
                        .where(Domain.reseller_id == reseller_id, Mailbox.used_bytes != 0)
                    )
                ).all()
                assert not drift, f"used_bytes drifted: {drift}"
                print("ok   used_bytes reconciles to zero on every mailbox")

            print("\n10 checks passed")
        finally:
            msg_ids = (
                await session.scalars(
                    select(Message.id).where(Message.reseller_id == reseller_id)
                )
            ).all()
            if msg_ids:
                await session.execute(
                    delete(ProcessedEvent).where(ProcessedEvent.message_id.in_(msg_ids))
                )
                await session.execute(
                    delete(MessageAttachment).where(MessageAttachment.message_id.in_(msg_ids))
                )
                await session.execute(
                    delete(MailboxMessage).where(MailboxMessage.message_id.in_(msg_ids))
                )
            await session.execute(delete(Message).where(Message.reseller_id == reseller_id))
            await session.execute(delete(BlobChunk).where(BlobChunk.reseller_id == reseller_id))
            await session.execute(delete(Blob).where(Blob.reseller_id == reseller_id))
            # Take the objects with the rows. This teardown deletes `chunk` rows behind
            # GC's back, so without this a run that fails before the final sweep strands
            # its bytes in the bucket with nothing left that knows they exist — exactly
            # the leak phase 3 exists to prevent, caused by the test for phase 3.
            leftover = (
                await session.scalars(
                    select(Chunk.s3_key).where(Chunk.reseller_id == reseller_id)
                )
            ).all()
            await session.execute(delete(Chunk).where(Chunk.reseller_id == reseller_id))
            if leftover:
                await storage.delete_chunks(list(leftover))
            await session.execute(delete(Domain).where(Domain.name == DOMAIN))
            await session.execute(delete(Reseller).where(Reseller.id == reseller_id))
            await session.commit()
