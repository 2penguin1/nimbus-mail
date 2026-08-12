"""Block D: the routing chain and the fan-out counters.

Needs the whole stack — Docker, the API, the receiver AND the worker.

One email, four recipients, four different branches of the chain, and then the numbers
that GC and quota depend on. The counters are the point: a wrong `blob.refcount` is not
a visible bug, it is data that garbage collection deletes months later.
"""

import asyncio
import secrets
import smtplib
import time
from datetime import datetime, timezone
from email.message import EmailMessage

import pytest
from sqlalchemy import delete, func, select

from nimbus import db
from nimbus.api import security
from nimbus.models import (
    Alias,
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
    SharedMailboxMember,
)
from nimbus.worker import routing

# Needs the whole stack: Docker, the API, the receiver AND the worker.
pytestmark = pytest.mark.integration

TAG = secrets.token_hex(4)
DOMAIN = f"route-{TAG}.example"
ATTACHMENT = (b"ROUTING-PAYLOAD-" * 400_000)[: 5 * 1024 * 1024]
assert len(ATTACHMENT) == 5 * 1024 * 1024


def send(to_addrs: list[str]) -> None:
    msg = EmailMessage()
    msg["From"] = "ceo@globex.com"
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = "routing check"
    msg.set_content("body")
    msg.add_attachment(
        ATTACHMENT, maintype="application", subtype="pdf", filename="deck.pdf"
    )
    with smtplib.SMTP("127.0.0.1", 2525, timeout=120) as s:
        s.send_message(msg, from_addr="ceo@globex.com", to_addrs=to_addrs)


async def wait_for_delivery(session, reseller_id, want: int, seconds: int = 90) -> int:
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


async def test_routing_chain_and_counters() -> None:
    async with db.Session() as session:
        key, key_hash = security.new_api_key()
        session.add(Reseller(name=f"route-{TAG}", api_key_hash=key_hash))
        await session.commit()
        reseller_id = await session.scalar(
            select(Reseller.id).where(Reseller.name == f"route-{TAG}")
        )

        try:
            domain = Domain(reseller_id=reseller_id, name=DOMAIN)
            session.add(domain)
            await session.flush()

            # alice   a plain mailbox                     <- reached via an ALIAS
            # team    a shared mailbox with two members   <- ONE copy, not three
            # bob/carol  members of team                  <- must receive NOTHING
            # catchall   the domain's catch-all           <- reached by an unknown name
            boxes = {}
            for name in ("alice", "team", "bob", "carol", "catchall"):
                mailbox = Mailbox(
                    domain_id=domain.id,
                    local_part=name,
                    password_hash=security.hash_password("pw"),
                    is_shared=(name == "team"),
                )
                session.add(mailbox)
                await session.flush()
                boxes[name] = mailbox.id

            domain.catch_all_mailbox_id = boxes["catchall"]
            session.add(Alias(domain_id=domain.id, local_part="sales",
                              target_mailbox_id=boxes["alice"]))
            for member in ("bob", "carol"):
                session.add(SharedMailboxMember(shared_mailbox_id=boxes["team"],
                                                member_mailbox_id=boxes[member]))
            await session.commit()

            # The receiver answers RCPT TO from Redis, so the addresses must be there.
            await db.connect_redis()
            await db.redis().sadd(
                "valid_addresses",
                f"sales@{DOMAIN}", f"team@{DOMAIN}", f"alice@{DOMAIN}",
            )
            await db.redis().sadd("catch_all_domains", DOMAIN)

            # sales@   -> alias      -> alice
            # team@    -> mailbox    -> team (ONE row; bob and carol get none)
            # alice@   -> mailbox    -> alice, ALREADY reached via the alias -> deduped
            # nobody@  -> catch-all  -> catchall
            send([f"sales@{DOMAIN}", f"team@{DOMAIN}", f"alice@{DOMAIN}",
                  f"nobody@{DOMAIN}"])

            delivered = await wait_for_delivery(session, reseller_id, want=3)
            assert delivered == 3, (
                f"expected 3 mailbox_message rows, got {delivered} — "
                "4 recipients, but alice is reached twice and must be counted once"
            )
            print("ok   4 recipients -> 3 mailboxes (alice deduplicated)")

            got = {
                name for (name,) in (
                    await session.execute(
                        select(Mailbox.local_part)
                        .join(MailboxMessage, MailboxMessage.mailbox_id == Mailbox.id)
                        .join(Message, Message.id == MailboxMessage.message_id)
                        .where(Message.reseller_id == reseller_id)
                    )
                ).all()
            }
            assert got == {"alice", "team", "catchall"}, f"delivered to {got}"
            print("ok   alias -> alice, mailbox -> team, unknown -> catch-all")

            assert "bob" not in got and "carol" not in got, \
                "shared mailbox was expanded to its members — it must not be"
            print("ok   shared mailbox members received NO copy of their own")

            # blob.refcount counts mailbox_message rows. 3 rows, one blob.
            # Plain values, not ORM objects: expire_all() below would make any attribute
            # access here lazy-load, and a lazy load inside async SQLAlchemy raises
            # MissingGreenlet rather than doing the IO.
            blobs = (
                await session.execute(
                    select(Blob.hash, Blob.refcount).where(Blob.reseller_id == reseller_id)
                )
            ).all()
            assert len(blobs) == 1, f"expected 1 blob, got {len(blobs)}"
            blob_hash, refcount = blobs[0]
            assert refcount == 3, (
                f"blob.refcount is {refcount}, expected 3 — it counts "
                "mailbox_message rows, not recipients"
            )
            print("ok   blob.refcount == 3, matching the rows written")

            size = await session.scalar(
                select(Message.size_bytes).where(Message.reseller_id == reseller_id)
            )
            for name in ("alice", "team", "catchall"):
                used = await session.scalar(
                    select(Mailbox.used_bytes).where(Mailbox.id == boxes[name])
                )
                assert used == size, f"{name} used_bytes={used}, expected {size}"
            for name in ("bob", "carol"):
                used = await session.scalar(
                    select(Mailbox.used_bytes).where(Mailbox.id == boxes[name])
                )
                assert used == 0, f"{name} was charged {used} bytes for a shared message"
            print("ok   used_bytes charged to the 3 recipients, not the 5 mailboxes")

            # The trigger is the only path down. Deleting one row must drop refcount to 2
            # and zero that mailbox's usage — the same arithmetic in reverse.
            await session.execute(
                delete(MailboxMessage).where(MailboxMessage.mailbox_id == boxes["alice"])
            )
            await session.commit()
            session.expire_all()
            after = await session.scalar(
                select(Blob.refcount).where(
                    Blob.reseller_id == reseller_id, Blob.hash == blob_hash
                )
            )
            assert after == 2, f"refcount is {after} after one delete, expected 2"
            print("ok   deleting one row -> refcount 3 -> 2 (the trigger, in reverse)")

            print("\n7 checks passed")
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
            await session.execute(delete(Message).where(Message.reseller_id == reseller_id))
            await session.execute(delete(BlobChunk).where(BlobChunk.reseller_id == reseller_id))
            await session.execute(delete(Blob).where(Blob.reseller_id == reseller_id))
            await session.execute(delete(Chunk).where(Chunk.reseller_id == reseller_id))
            await session.execute(delete(Domain).where(Domain.name == DOMAIN))
            await session.execute(delete(Reseller).where(Reseller.id == reseller_id))
            await session.commit()
            if db.redis() is not None:
                await db.redis().srem(
                    "valid_addresses",
                    f"sales@{DOMAIN}", f"team@{DOMAIN}", f"alice@{DOMAIN}",
                )
                await db.redis().srem("catch_all_domains", DOMAIN)


async def test_deliver_is_idempotent_and_stamps_arrival_time() -> None:
    """Call resolve()/deliver() directly — no receiver, no Kafka, no worker.

    The end-to-end test above cannot reach either of these. A replay is Kafka's normal
    behaviour, and it must add ZERO to blob.refcount the second time; a refcount that
    never falls back to zero is a blob GC can never free. And an inbox sorts on
    `mailbox_message.received_at` (docs/DATABASE.md §5), so that column has to hold when
    the mail ARRIVED — not when a backlog drain got round to it.
    """
    arrived = datetime(2020, 1, 1, 12, 0, tzinfo=timezone.utc)
    tag = secrets.token_hex(4)
    async with db.Session() as session:
        _, key_hash = security.new_api_key()
        reseller = Reseller(name=f"direct-{tag}", api_key_hash=key_hash)
        session.add(reseller)
        await session.flush()
        try:
            domain = Domain(reseller_id=reseller.id, name=f"direct-{tag}.example")
            session.add(domain)
            await session.flush()
            alice = Mailbox(domain_id=domain.id, local_part="alice")
            session.add(alice)
            await session.flush()
            session.add(Alias(domain_id=domain.id, local_part="sales",
                              target_mailbox_id=alice.id))
            message = Message(reseller_id=reseller.id, raw_s3_key=f"direct/{tag}",
                              size_bytes=4242, received_at=arrived)
            session.add(message)
            await session.flush()

            # Two addresses, one mailbox: the set must collapse them to a single row.
            targets = await routing.resolve(
                session, reseller.id,
                [f"sales@{domain.name}", f"alice@{domain.name}"],
            )
            assert targets == {alice.id}, targets

            first = await routing.deliver(
                session, reseller.id, message.id, targets, 4242, arrived)
            replay = await routing.deliver(
                session, reseller.id, message.id, targets, 4242, arrived)
            # deliver() returns the SET of mailboxes it wrote, because block F's search
            # index has to know which ones to index. The replay must return it empty.
            assert (first, replay) == ({alice.id}, set()), (
                f"deliver wrote {first} then {replay} — a replay must write none"
            )

            used = await session.scalar(
                select(Mailbox.used_bytes).where(Mailbox.id == alice.id))
            assert used == 4242, f"used_bytes={used}, the replay charged it twice"

            stamped = await session.scalar(
                select(MailboxMessage.received_at).where(
                    MailboxMessage.message_id == message.id))
            assert stamped == arrived, (
                f"mailbox_message.received_at is {stamped}, expected {arrived} — "
                "an inbox would sort by processing time instead of arrival time"
            )
            print("ok   replay writes 0 rows, charges 0 bytes, arrival time preserved")
        finally:
            await session.rollback()
            await session.execute(delete(Domain).where(Domain.reseller_id == reseller.id))
            await session.execute(delete(Reseller).where(Reseller.id == reseller.id))
            await session.commit()
