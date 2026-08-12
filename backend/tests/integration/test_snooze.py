"""Block H: snooze. Needs Docker, the API, the SMTP receiver AND the worker.

Snooze has exactly two failure modes and they pull in opposite directions:

  **never fires** — the mail is hidden for ever. To the user that is lost mail, and it is
  the worse of the two, because nothing surfaces it again.
  **wrongly fires** — the mail comes back early, so the feature silently did not work.

There is no unit test here and that is a real gap: the whole of block H is one SQL
predicate, so nothing can be checked without a database. Blocks E and F both had a pure
module to test offline; this one does not.
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
)

pytestmark = pytest.mark.integration

API = "http://127.0.0.1:8000"
TAG = secrets.token_hex(4)
DOMAIN = f"snooze-{TAG}.example"


def send(to_addr: str, subject: str) -> None:
    msg = EmailMessage()
    msg["From"] = "sender@globex.com"
    msg["To"] = to_addr
    msg["Subject"] = subject
    msg.set_content("body text")
    with smtplib.SMTP("127.0.0.1", 2525, timeout=60) as s:
        s.send_message(msg, from_addr="sender@globex.com", to_addrs=[to_addr])


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


def _in(seconds: float) -> str:
    when = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=seconds)
    return when.isoformat()


async def test_snooze() -> None:
    async with db.Session() as session:
        key, key_hash = security.new_api_key()
        session.add(Reseller(name=f"snooze-{TAG}", api_key_hash=key_hash))
        await session.commit()
        reseller_id = await session.scalar(
            select(Reseller.id).where(Reseller.name == f"snooze-{TAG}")
        )

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                order = await client.post(
                    f"{API}/v1/orders",
                    json={"domain": DOMAIN, "mailboxes": ["alice"]},
                    headers={"Authorization": f"Bearer {key}", "Idempotency-Key": TAG},
                )
                assert order.status_code == 201, order.text
                password = order.json()["mailboxes"][0]["temp_password"]

                send(f"alice@{DOMAIN}", "Quarterly report")
                assert await wait_for(session, reseller_id, want=1) == 1

                r = await client.post(
                    f"{API}/v1/auth/login",
                    json={"address": f"alice@{DOMAIN}", "password": password},
                )
                assert r.status_code == 200, r.text
                alice = {"Authorization": f"Bearer {r.json()['token']}"}

                async def inbox(**params):
                    r = await client.get(f"{API}/v1/messages", params=params, headers=alice)
                    assert r.status_code == 200, r.text
                    return [m["subject"] for m in r.json()["messages"]]

                assert await inbox() == ["Quarterly report"]
                message_id = (
                    await client.get(f"{API}/v1/messages", headers=alice)
                ).json()["messages"][0]["id"]
                print("ok   the message is in the inbox to begin with")

                # ---- 1. snoozing hides it ----------------------------------------
                r = await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice,
                    json={"snooze_until": _in(3600)},
                )
                assert r.status_code == 200, r.text
                assert await inbox() == [], "a snoozed message is still in the inbox"
                print("ok   snoozed for an hour: gone from the inbox")

                # ---- 2. but it is still findable ---------------------------------
                assert await inbox(snoozed=True) == ["Quarterly report"], (
                    "?snoozed=true does not surface it — the user cannot un-snooze early"
                )
                r = await client.get(
                    f"{API}/v1/search", params={"q": "Quarterly"}, headers=alice
                )
                assert [m["subject"] for m in r.json()["messages"]] == ["Quarterly report"], (
                    "search hid a snoozed message — HLD §9.4 says it must not"
                )
                r = await client.get(f"{API}/v1/messages/{message_id}", headers=alice)
                assert r.status_code == 200, "a snoozed message must still open by id"
                assert r.json()["snooze_until"] is not None
                print("ok   still reachable by ?snoozed=true, by search, and by id")

                # ---- 3. un-snoozing early ----------------------------------------
                # `{"snooze_until": null}` — the case `is not None` cannot express.
                r = await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice,
                    json={"snooze_until": None},
                )
                assert r.status_code == 200, r.text
                assert await inbox() == ["Quarterly report"], (
                    "explicit null did not un-snooze — model_fields_set is not being used"
                )
                print("ok   {\"snooze_until\": null} brings it straight back")

                # ---- 4. an unrelated PATCH must not clear a snooze ----------------
                await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice,
                    json={"snooze_until": _in(3600)},
                )
                r = await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice, json={"is_read": True}
                )
                assert r.status_code == 200, r.text
                assert await inbox() == [], (
                    "marking a snoozed message read un-snoozed it — an omitted field was "
                    "treated as null"
                )
                print("ok   marking it read leaves the snooze alone")

                # ---- 5. it comes back on its own, with nothing running ------------
                # The whole of block H. No worker, no poll, no timer store: the predicate
                # simply stops being true.
                r = await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice,
                    json={"snooze_until": _in(3)},
                )
                assert r.status_code == 200, r.text
                assert await inbox() == [], "not hidden immediately after snoozing"
                await asyncio.sleep(4)
                assert await inbox() == ["Quarterly report"], (
                    "the message never came back — snooze fired late or not at all"
                )
                print("ok   returned by itself 3s later, with no worker running")

                # ---- 6. validation ------------------------------------------------
                naive = (
                    datetime.datetime.now() + datetime.timedelta(hours=1)
                ).replace(tzinfo=None).isoformat()
                r = await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice,
                    json={"snooze_until": naive},
                )
                assert r.status_code == 422, (
                    f"a naive datetime gave {r.status_code}, not 422 — it would be read in "
                    "the SERVER's timezone and fire hours early, silently"
                )
                r = await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice,
                    json={"snooze_until": _in(-60)},
                )
                assert r.status_code == 400, f"a past time gave {r.status_code}"
                r = await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice,
                    json={"snooze_until": _in(400 * 86400)},
                )
                assert r.status_code == 400, f"a 400-day snooze gave {r.status_code}"
                print("ok   naive 422, past 400, absurdly-far-future 400")

                # ---- 7. snooze is per copy, and costs no bytes --------------------
                before = await session.scalar(
                    select(Mailbox.used_bytes)
                    .join(Domain, Domain.id == Mailbox.domain_id)
                    .where(Domain.name == DOMAIN)
                )
                await client.patch(
                    f"{API}/v1/messages/{message_id}", headers=alice,
                    json={"snooze_until": _in(3600)},
                )
                session.expire_all()
                after = await session.scalar(
                    select(Mailbox.used_bytes)
                    .join(Domain, Domain.id == Mailbox.domain_id)
                    .where(Domain.name == DOMAIN)
                )
                assert before == after, (
                    f"used_bytes moved {before} -> {after} on a snooze — the delete "
                    "trigger is firing on UPDATE, which would corrupt every counter"
                )
                print("ok   snoozing moves no counters (the trigger is DELETE-only)")

            print("\n11 checks passed")
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
