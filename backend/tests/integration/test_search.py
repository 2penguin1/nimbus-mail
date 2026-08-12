"""Block F: search, end to end through the real stack.

Needs Docker, the API, the SMTP receiver AND the worker.

Four things here cannot be checked any other way:

  1. **Isolation.** Bob must not find Alice's mail, even when both messages contain the
     same words. A search engine is the easiest place in a system to leak a tenant,
     because one forgotten filter returns everything instead of failing.
  2. **A shared mailbox's mail is findable.** Block D delivers ONE copy to a shared
     mailbox; its members read it. If search filtered on the token's own mailbox id, that
     mail would be permanently unfindable — by the members, and by the mailbox itself,
     which has no password and cannot be logged into. ARCHITECTURE diagram 16 had exactly
     this bug drawn into it.
  3. **A deleted copy stops being findable.** Nothing in the application deletes a
     `message_index` row; a composite foreign key does. If that cascade is wrong, deleted
     mail keeps appearing in results and opening it 404s.
  4. **Hostile input does not 500.** `to_tsquery` raises on `foo &`. The whole reason
     this uses `websearch_to_tsquery` is that a typo must not be a server error.
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
    MessageIndex,
    ProcessedEvent,
    Reseller,
    SharedMailboxMember,
)

pytestmark = pytest.mark.integration

API = "http://127.0.0.1:8000"
TAG = secrets.token_hex(4)
DOMAIN = f"search-{TAG}.example"


def send(to_addrs: list[str], subject: str, body: str, attach: str | None = None) -> None:
    msg = EmailMessage()
    msg["From"] = "cfo@globex.com"
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content(body)
    if attach:
        msg.add_attachment(
            b"PDF-BYTES-" * 100, maintype="application", subtype="pdf", filename=attach
        )
    with smtplib.SMTP("127.0.0.1", 2525, timeout=60) as s:
        s.send_message(msg, from_addr="cfo@globex.com", to_addrs=to_addrs)


async def wait_for_rows(session, reseller_id, want: int, seconds: int = 90) -> int:
    """Wait on message_index, not mailbox_message — indexing is the last step."""
    deadline = time.monotonic() + seconds
    got = 0
    while time.monotonic() < deadline:
        session.expire_all()
        got = await session.scalar(
            select(func.count())
            .select_from(MessageIndex)
            .join(Message, Message.id == MessageIndex.message_id)
            .where(Message.reseller_id == reseller_id)
        )
        if got >= want:
            return got
        await asyncio.sleep(2)
    return got


async def test_search() -> None:
    async with db.Session() as session:
        key, key_hash = security.new_api_key()
        session.add(Reseller(name=f"search-{TAG}", api_key_hash=key_hash))
        await session.commit()
        reseller_id = await session.scalar(
            select(Reseller.id).where(Reseller.name == f"search-{TAG}")
        )

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                order = await client.post(
                    f"{API}/v1/orders",
                    json={"domain": DOMAIN, "mailboxes": ["alice", "support", "bob"]},
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

                # Bob's message carries the SAME distinctive word as Alice's. If any
                # filter is missing, his copy shows up in her results.
                send([f"alice@{DOMAIN}"], "Quarterly invoice", "please review", "report.pdf")
                send([f"support@{DOMAIN}"], "Team standup notes", "nothing attached")
                send([f"bob@{DOMAIN}"], "Quarterly invoice for bob", "private")
                # One email, two recipients, one of them a shared mailbox Alice reads.
                # That is 1 message row and 2 mailbox_message rows — both hers to see.
                send([f"alice@{DOMAIN}", f"support@{DOMAIN}"], "Budget planning", "figures")

                # 5 index rows: alice 1, support 1, bob 1, and the last mail's 2 copies.
                assert await wait_for_rows(session, reseller_id, want=5) == 5
                print("ok   5 index rows written, one per delivered copy")

                async def token_for(local: str) -> str:
                    r = await client.post(
                        f"{API}/v1/auth/login",
                        json={"address": f"{local}@{DOMAIN}", "password": passwords[local]},
                    )
                    assert r.status_code == 200, r.text
                    return r.json()["token"]

                alice = {"Authorization": f"Bearer {await token_for('alice')}"}
                bob = {"Authorization": f"Bearer {await token_for('bob')}"}

                async def find(headers, q):
                    r = await client.get(f"{API}/v1/search", params={"q": q}, headers=headers)
                    assert r.status_code == 200, f"{q!r} -> {r.status_code} {r.text}"
                    return r.json()["messages"]

                # ---- 1. it finds anything at all ---------------------------------
                hits = await find(alice, "invoice")
                assert [h["subject"] for h in hits] == ["Quarterly invoice"], hits
                print("ok   free-text search finds the message by a word in its subject")

                assert await find(alice, "review"), "a word from the BODY must match"
                print("ok   the body is indexed, not just the headers")

                assert await find(alice, "report.pdf"), "an attachment filename must match"
                print("ok   attachment filenames are searchable")

                # ---- 2. isolation — the check that matters -----------------------
                hits = await find(bob, "invoice")
                assert [h["subject"] for h in hits] == ["Quarterly invoice for bob"], hits
                print("ok   bob's search for the same word returns only his own mail")

                assert await find(bob, "review") == [], "bob matched alice's body text"
                assert await find(bob, "standup") == [], "bob matched the shared mailbox"
                print("ok   bob finds nothing of alice's or the team's")

                # ---- 3. the shared mailbox is findable by its members -------------
                hits = await find(alice, "standup")
                assert [h["subject"] for h in hits] == ["Team standup notes"], (
                    "alice cannot find the shared mailbox's mail — this is the bug that "
                    "would make every team inbox permanently unsearchable"
                )
                assert hits[0]["mailbox_id"] == str(boxes["support"])
                print("ok   alice finds the SHARED mailbox's mail, tagged with its id")

                # ---- 4. two copies of one message are two rows, not four ----------
                hits = await find(alice, "budget")
                assert len(hits) == 2, (
                    f"expected 2 copies, got {len(hits)} — joining message_index on "
                    "message_id alone makes this a cartesian product"
                )
                assert {h["mailbox_id"] for h in hits} == {
                    str(boxes["alice"]), str(boxes["support"])
                }
                print("ok   a message in two readable mailboxes returns exactly 2 rows")

                # ---- 5. the operators --------------------------------------------
                assert len(await find(alice, "from:cfo")) == 4, "from: should match all 4"
                assert await find(alice, "from:nobody@nowhere") == []
                print("ok   from: filters on the sender")

                hits = await find(alice, "has:attachment")
                assert [h["subject"] for h in hits] == ["Quarterly invoice"], hits
                assert hits[0]["has_attachments"] is True
                print("ok   has:attachment finds only the message that carries one")

                assert len(await find(alice, "after:2020-01-01")) == 4
                assert await find(alice, "before:2020-01-01") == []
                print("ok   before: and after: filter on arrival time")

                hits = await find(alice, "from:cfo has:attachment invoice")
                assert [h["subject"] for h in hits] == ["Quarterly invoice"]
                print("ok   operators and free text combine (the HLD §9.4 example)")

                # ---- 6. hostile input is answered, not crashed -------------------
                for hostile in ["foo &", "!", "&&&", "'; DROP TABLE message; --",
                                '"unclosed', "-", "a:b:c", "%_%",
                                # NUL. Postgres TEXT cannot hold 0x00 and asyncpg raises
                                # on it, so without a strip in parse() this is a 500 that
                                # anyone can send. The first version of this list missed
                                # it while claiming to cover hostile input.
                                "\x00abc", "from:a\x00b"]:
                    r = await client.get(f"{API}/v1/search", params={"q": hostile},
                                         headers=alice)
                    assert r.status_code == 200, (
                        f"{hostile!r} returned {r.status_code} — to_tsquery raises on "
                        "input like this, which is why websearch_to_tsquery is used"
                    )
                print("ok   10 hostile queries all answered 200, none crashed the server")

                # ---- 6b. pagination -----------------------------------------------
                # The cursor is the one piece shared with GET /v1/messages, and the
                # symptom of getting it wrong is a row that silently never appears on
                # any page — invisible in testing, permanent in production. Walk the
                # whole result set one row at a time and check nothing is lost or
                # repeated. `from:cfo` matches all 4 copies.
                seen, next_cursor, pages = [], None, 0
                while pages < 10:
                    params = {"q": "from:cfo", "limit": 1}
                    if next_cursor:
                        params["cursor"] = next_cursor
                    page = await client.get(f"{API}/v1/search", params=params, headers=alice)
                    assert page.status_code == 200, page.text
                    body = page.json()
                    seen.extend((m["id"], m["mailbox_id"]) for m in body["messages"])
                    next_cursor = body["next_cursor"]
                    pages += 1
                    if not next_cursor:
                        break
                assert len(seen) == 4, f"paged through {len(seen)} rows, expected 4"
                assert len(set(seen)) == 4, f"a row was returned twice: {seen}"
                print(f"ok   paged all 4 copies one at a time over {pages} pages, none lost")

                r = await client.get(
                    f"{API}/v1/search", params={"q": "invoice", "cursor": "not-base64!"},
                    headers=alice,
                )
                assert r.status_code == 400, f"a bad cursor gave {r.status_code}"
                print("ok   a malformed cursor is 400, not 500")

                r = await client.get(f"{API}/v1/search", params={"q": "   "}, headers=alice)
                assert r.status_code == 400, "an empty query must not return the inbox"
                print("ok   an empty query is 400, not a full inbox dump")

                # ---- 7. deleting a copy removes it from search --------------------
                target = (await find(alice, "invoice"))[0]
                r = await client.delete(f"{API}/v1/messages/{target['id']}", headers=alice)
                assert r.status_code == 204, r.text
                assert await find(alice, "invoice") == [], (
                    "a deleted message is still searchable — the composite foreign key "
                    "onto mailbox_message is not cascading"
                )
                print("ok   deleting a copy takes its index row with it")

                # Deleting ALICE's copy of the shared message must not touch the
                # shared mailbox's copy — that is a different row and a different team.
                budget = (await find(alice, "budget"))[0]["id"]
                r = await client.delete(
                    f"{API}/v1/messages/{budget}",
                    params={"mailbox_id": str(boxes["alice"])},
                    headers=alice,
                )
                assert r.status_code == 204, r.text
                survivors = await find(alice, "budget")
                assert [h["mailbox_id"] for h in survivors] == [str(boxes["support"])], (
                    "deleting a personal copy also removed the team's copy from search"
                )
                print("ok   deleting one copy leaves the other mailbox's copy findable")

            print("\n18 checks passed")
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
            await session.execute(delete(Chunk).where(Chunk.reseller_id == reseller_id))
            await session.execute(delete(Domain).where(Domain.name == DOMAIN))
            await session.execute(delete(Reseller).where(Reseller.id == reseller_id))
            await session.commit()
