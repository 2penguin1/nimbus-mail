"""One message in two of my mailboxes — the case that makes a message id ambiguous.

Needs Docker, the API, the SMTP receiver AND the worker.

A sender CCs `alice@` and `support@` on one email. Alice belongs to `support@`, so
routing writes TWO `mailbox_message` rows and both are readable by her. Everything that
treats a message id as if it addressed a row breaks here — most dangerously `DELETE`,
where guessing wrong destroys a team's only copy of an email with no undo.

`mailbox_message`'s primary key is `(mailbox_id, message_id)`. This file is the proof
that the API respects that.
"""

import asyncio
import secrets
import smtplib
import time
import uuid
from email.message import EmailMessage

import httpx
import pytest
from sqlalchemy import delete, func, select

from nimbus import db
from nimbus.api import security
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
    SharedMailboxMember,
)

pytestmark = pytest.mark.integration

API = "http://127.0.0.1:8000"
TAG = secrets.token_hex(4)
DOMAIN = f"dup-{TAG}.example"
PAYLOAD = (b"SHARED-MAILBOX-PAYLOAD-" * 100_000)[: 1024 * 1024]


async def wait_for(session, reseller_id, want, seconds=90):
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


async def test_one_message_in_two_of_my_mailboxes() -> None:
    async with db.Session() as session:
        key, key_hash = security.new_api_key()
        session.add(Reseller(name=f"dup-{TAG}", api_key_hash=key_hash))
        await session.commit()
        reseller_id = await session.scalar(
            select(Reseller.id).where(Reseller.name == f"dup-{TAG}")
        )
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                order = await client.post(
                    f"{API}/v1/orders",
                    json={"domain": DOMAIN, "mailboxes": ["alice", "support"]},
                    headers={"Authorization": f"Bearer {key}", "Idempotency-Key": TAG},
                )
                assert order.status_code == 201, order.text
                passwords = {
                    m["local_part"]: m["temp_password"] for m in order.json()["mailboxes"]
                }

                boxes = {
                    local: mid
                    for local, mid in (
                        await session.execute(
                            select(Mailbox.local_part, Mailbox.id)
                            .join(Domain, Domain.id == Mailbox.domain_id)
                            .where(Domain.name == DOMAIN)
                        )
                    ).all()
                }
                # No API creates a shared mailbox yet — see the gap noted in HLD §9.2.
                # The test writes the rows the missing endpoint would write.
                await session.execute(
                    Mailbox.__table__.update()
                    .where(Mailbox.id == boxes["support"])
                    .values(is_shared=True)
                )
                session.add(
                    SharedMailboxMember(
                        shared_mailbox_id=boxes["support"],
                        member_mailbox_id=boxes["alice"],
                    )
                )
                await session.commit()

                message = EmailMessage()
                message["From"] = "ceo@globex.com"
                message["To"] = f"alice@{DOMAIN}, support@{DOMAIN}"
                message["Subject"] = "copied to both"
                message.set_content("hello")
                message.add_attachment(
                    PAYLOAD, maintype="application", subtype="pdf", filename="both.pdf"
                )
                with smtplib.SMTP("127.0.0.1", 2525, timeout=120) as smtp:
                    smtp.send_message(
                        message,
                        from_addr="ceo@globex.com",
                        to_addrs=[f"alice@{DOMAIN}", f"support@{DOMAIN}"],
                    )
                assert await wait_for(session, reseller_id, want=2) == 2
                print("ok   one email -> two rows (own mailbox + shared mailbox)")

                login = await client.post(
                    f"{API}/v1/auth/login",
                    json={"address": f"alice@{DOMAIN}", "password": passwords["alice"]},
                )
                alice = {"Authorization": f"Bearer {login.json()['token']}"}

                listed = (await client.get(f"{API}/v1/messages", headers=alice)).json()
                assert len(listed["messages"]) == 2, "both copies should be listed"
                assert len({m["id"] for m in listed["messages"]}) == 1, "one message"
                assert len({m["mailbox_id"] for m in listed["messages"]}) == 2
                message_id = listed["messages"][0]["id"]
                print("ok   two copies listed: one message id, two mailbox ids")

                # An ambiguous write must refuse rather than guess.
                r = await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice, json={"is_read": True}
                )
                assert r.status_code == 409, f"ambiguous patch returned {r.status_code}"
                r = await client.delete(f"{API}/v1/messages/{message_id}", headers=alice)
                assert r.status_code == 409, f"ambiguous delete returned {r.status_code}"
                print("ok   an unqualified write across two copies is refused with 409")

                own = boxes["alice"]
                shared = boxes["support"]

                r = await client.patch(
                    f"{API}/v1/messages/{message_id}?mailbox_id={own}",
                    headers=alice,
                    json={"is_read": True},
                )
                assert r.status_code == 200, r.text
                states = {
                    m["mailbox_id"]: m["is_read"]
                    for m in (
                        await client.get(f"{API}/v1/messages", headers=alice)
                    ).json()["messages"]
                }
                assert states[str(own)] is True
                assert states[str(shared)] is False, \
                    "marking her own copy read also marked the TEAM's copy read"
                print("ok   patch touches one copy; the team's copy is untouched")

                # The destructive one. Before the fix this removed both rows.
                r = await client.delete(
                    f"{API}/v1/messages/{message_id}?mailbox_id={own}", headers=alice
                )
                assert r.status_code == 204, r.text
                left = await session.scalar(
                    select(func.count())
                    .select_from(MailboxMessage)
                    .where(MailboxMessage.message_id == uuid.UUID(message_id))
                )
                assert left == 1, f"{left} copies left — the team's copy went with hers"
                print("ok   deleting her copy leaves the team's copy intact")

                # Still readable through the shared mailbox, and now unambiguous.
                detail = await client.get(f"{API}/v1/messages/{message_id}", headers=alice)
                assert detail.status_code == 200, detail.text
                thread_id = detail.json()["thread_id"]
                r = await client.get(f"{API}/v1/threads/{thread_id}", headers=alice)
                assert r.status_code == 200, r.text
                assert len(r.json()["messages"]) == 1, "thread showed the message twice"
                print("ok   thread shows the message once, not once per copy")

                url = f"{API}{detail.json()['attachments'][0]['url']}"
                first = await client.get(url, headers=alice)
                assert first.status_code == 200
                assert first.content == PAYLOAD
                etag = first.headers["etag"]
                again = await client.get(url, headers={**alice, "If-None-Match": etag})
                assert again.status_code == 304, f"expected 304, got {again.status_code}"
                assert again.content == b""
                print("ok   re-fetch with the ETag is a 304, not another download")

            print("\n8 checks passed")
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
            await session.execute(delete(Message).where(Message.reseller_id == reseller_id))
            await session.execute(delete(BlobChunk).where(BlobChunk.reseller_id == reseller_id))
            await session.execute(delete(Blob).where(Blob.reseller_id == reseller_id))
            await session.execute(delete(Chunk).where(Chunk.reseller_id == reseller_id))
            await session.execute(delete(Domain).where(Domain.name == DOMAIN))
            await session.execute(delete(Reseller).where(Reseller.id == reseller_id))
            await session.commit()
