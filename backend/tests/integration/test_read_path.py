"""Block E: reading mail back out, end to end through the real stack.

Needs Docker, the API, the SMTP receiver AND the worker.

The two things worth proving here cannot be checked without a live system:

  1. **Isolation.** Bob must not see Alice's mail, even though they are in the same
     domain, at the same reseller, and the attachment bytes are physically SHARED
     between them by the dedup engine. This is the failure that matters most.
  2. **The bytes come back intact.** A chunked, deduplicated, range-served attachment
     must reassemble byte-for-byte into what the sender attached. Everything upstream is
     pointless if it does not.
"""

import asyncio
import secrets
import smtplib
import time
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

# Needs the whole stack: Docker, the API, the receiver AND the worker.
pytestmark = pytest.mark.integration

API = "http://127.0.0.1:8000"
TAG = secrets.token_hex(4)
DOMAIN = f"read-{TAG}.example"

# 5 MB: more than one 4 MB chunk, so a range request has to span a chunk boundary —
# which is the whole reason ranges.slice_chunks exists.
_WANT = 5 * 1024 * 1024
_FILLER = b"READ-PATH-PAYLOAD-"
PAYLOAD = (_FILLER * (_WANT // len(_FILLER) + 1))[:_WANT]
assert len(PAYLOAD) == _WANT


def send(to_addr: str, subject: str) -> None:
    msg = EmailMessage()
    msg["From"] = "ceo@globex.com"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content("see attached")
    msg.add_attachment(
        PAYLOAD, maintype="application", subtype="pdf", filename="report.pdf"
    )
    with smtplib.SMTP("127.0.0.1", 2525, timeout=120) as s:
        s.send_message(msg, from_addr="ceo@globex.com", to_addrs=[to_addr])


async def wait_for(session, reseller_id, want: int, seconds: int = 90) -> int:
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


async def test_read_path(verify_domain) -> None:
    async with db.Session() as session:
        key, key_hash = security.new_api_key()
        session.add(Reseller(name=f"read-{TAG}", api_key_hash=key_hash))
        await session.commit()
        reseller_id = await session.scalar(
            select(Reseller.id).where(Reseller.name == f"read-{TAG}")
        )

        try:
            async with httpx.AsyncClient(timeout=120) as client:
                order = await client.post(
                    f"{API}/v1/orders",
                    json={"domain": DOMAIN, "mailboxes": ["alice", "support", "bob"]},
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

                # support@ becomes a SHARED mailbox with alice as a member. Delivery
                # writes ONE row to it; alice must see that row on READ (HLD §9.2).
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

                send(f"alice@{DOMAIN}", "for alice")
                send(f"support@{DOMAIN}", "for the team")
                assert await wait_for(session, reseller_id, want=2) == 2
                print("ok   two messages delivered")

                async def token_for(local: str) -> str:
                    r = await client.post(
                        f"{API}/v1/auth/login",
                        json={"address": f"{local}@{DOMAIN}", "password": passwords[local]},
                    )
                    assert r.status_code == 200, r.text
                    return r.json()["token"]

                alice = {"Authorization": f"Bearer {await token_for('alice')}"}
                bob = {"Authorization": f"Bearer {await token_for('bob')}"}

                # ---- 1. the inbox, including the shared mailbox -------------------
                r = await client.get(f"{API}/v1/messages", headers=alice)
                assert r.status_code == 200, r.text
                subjects = {m["subject"] for m in r.json()["messages"]}
                assert subjects == {"for alice", "for the team"}, subjects
                print("ok   alice sees her own mail AND the shared mailbox's")

                # ---- 2. isolation — the check that matters ------------------------
                r = await client.get(f"{API}/v1/messages", headers=bob)
                assert r.status_code == 200, r.text
                assert r.json()["messages"] == [], "bob can see mail that is not his"
                print("ok   bob sees nothing: same domain, same reseller, shared bytes")

                listed = (await client.get(f"{API}/v1/messages", headers=alice)).json()
                mine = next(m for m in listed["messages"] if m["subject"] == "for alice")
                assert mine["has_attachments"] is True

                # ---- 3. one message, with attachment metadata --------------------
                r = await client.get(f"{API}/v1/messages/{mine['id']}", headers=alice)
                assert r.status_code == 200, r.text
                # The body — what the email actually SAYS. Stored at delivery, not
                # re-parsed from S3 per request (which costs 187 MB per 25 MB message).
                assert r.json()["body_text"].strip() == "see attached", (
                    f"body_text was {r.json()['body_text']!r} — an email you cannot "
                    "read is not a read path"
                )
                print("ok   the message body comes back")

                attachment = r.json()["attachments"][0]
                assert attachment["filename"] == "report.pdf"
                assert attachment["size_bytes"] == len(PAYLOAD)
                print(f"ok   attachment listed: {attachment['filename']}, "
                      f"{attachment['size_bytes'] / 1024 / 1024:.0f} MB")

                # Bob must not reach it by id either, even knowing the blob hash.
                r = await client.get(f"{API}/v1/messages/{mine['id']}", headers=bob)
                assert r.status_code == 404, f"bob got {r.status_code} on alice's message"
                print("ok   bob gets 404 on alice's message, not 403")

                url = f"{API}{attachment['url']}"

                # ---- 4. the whole file, reassembled from its chunks ---------------
                r = await client.get(url, headers=alice)
                assert r.status_code == 200, r.text
                assert r.content == PAYLOAD, (
                    f"download is {len(r.content)} bytes, sent {len(PAYLOAD)} — "
                    "the chunk map reassembled wrong"
                )
                assert r.headers["accept-ranges"] == "bytes"
                print("ok   full download is byte-identical to what was sent")

                # Bob must not download it either — the bytes are physically shared.
                r = await client.get(url, headers=bob)
                assert r.status_code == 404, f"bob downloaded alice's attachment ({r.status_code})"
                print("ok   bob cannot download the shared bytes")

                # A blob hash is user input in the URL path. A NUL cannot be encoded into
                # Postgres TEXT, so without a format check asyncpg raises and this is a
                # 500 on a URL anyone can type.
                for bad in ["%00", "nothex" + "0" * 58, "abc"]:
                    r = await client.get(
                        f"{API}/v1/messages/{mine['id']}/attachments/{bad}", headers=alice
                    )
                    assert r.status_code == 404, f"blob_hash {bad!r} gave {r.status_code}"
                print("ok   a malformed blob hash is 404, not 500")

                # ---- 5. ranges ---------------------------------------------------
                # A window straddling the 4 MB chunk boundary — the case the whole
                # slice_chunks function exists for.
                boundary = 4 * 1024 * 1024
                start, end = boundary - 10, boundary + 9
                r = await client.get(url, headers={**alice, "Range": f"bytes={start}-{end}"})
                assert r.status_code == 206, r.text
                assert r.content == PAYLOAD[start:end + 1], "wrong bytes across the boundary"
                assert r.headers["content-range"] == f"bytes {start}-{end}/{len(PAYLOAD)}"
                print("ok   range spanning two chunks returns exactly the right 20 bytes")

                r = await client.get(url, headers={**alice, "Range": "bytes=-500"})
                assert r.status_code == 206
                assert r.content == PAYLOAD[-500:], "suffix range returned the wrong end"
                print("ok   suffix range returns the LAST 500 bytes")

                r = await client.get(
                    url, headers={**alice, "Range": f"bytes={len(PAYLOAD) + 1}-"}
                )
                assert r.status_code == 416, f"expected 416, got {r.status_code}"
                assert r.headers["content-range"] == f"bytes */{len(PAYLOAD)}"
                print("ok   a range past the end is refused with 416 and the real length")

                # ---- 6. read state and folders -----------------------------------
                r = await client.patch(
                    f"{API}/v1/messages/{mine['id']}", headers=alice,
                    json={"is_read": True, "folder": "archive"},
                )
                assert r.status_code == 200, r.text
                r = await client.get(f"{API}/v1/messages?folder=archive", headers=alice)
                assert [m["subject"] for m in r.json()["messages"]] == ["for alice"]
                r = await client.get(f"{API}/v1/messages?folder=inbox", headers=alice)
                assert "for alice" not in [m["subject"] for m in r.json()["messages"]]
                print("ok   patch moves it between folders and marks it read")

                r = await client.patch(
                    f"{API}/v1/messages/{mine['id']}", headers=bob, json={"is_read": True}
                )
                assert r.status_code == 404, "bob modified alice's message"
                print("ok   bob cannot patch alice's message")

                # ---- 7. delete drops the refcount --------------------------------
                before = await session.scalar(
                    select(Blob.refcount).where(Blob.reseller_id == reseller_id)
                )
                r = await client.delete(f"{API}/v1/messages/{mine['id']}", headers=alice)
                assert r.status_code == 204, r.text
                session.expire_all()
                after = await session.scalar(
                    select(Blob.refcount).where(Blob.reseller_id == reseller_id)
                )
                assert after == before - 1, (
                    f"refcount {before} -> {after}; deleting a copy must release exactly one"
                )
                r = await client.get(f"{API}/v1/messages/{mine['id']}", headers=alice)
                assert r.status_code == 404
                print(f"ok   delete removes her copy and drops refcount {before} -> {after}")

            print("\n12 checks passed")
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
