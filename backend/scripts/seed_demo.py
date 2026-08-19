"""Fill a local stack with realistic mail, through the real pipeline. Operator tool.

    cd backend
    uv run python scripts/seed_demo.py            # create and fill
    uv run python scripts/seed_demo.py --clean    # remove it again

**Why this exists.** Screenshotting or designing a mail UI against an empty inbox tells
you nothing, and against `lorem ipsum` it tells you the wrong thing — real mail has long
subjects, quoted replies, HTML that fights your stylesheet, and attachments with awkward
names. This produces mail with those properties so the UI is judged against what it will
actually hold.

Everything goes in over SMTP to the real receiver, so it exercises the whole path:
receiver -> S3 -> Kafka -> worker -> MIME split -> chunk -> dedup -> fan-out -> index.
Nothing is inserted straight into the database, because data that skipped the pipeline
would not have the shape the pipeline produces.

**It is deliberately NOT `loadtest.py`.** That one measures; this one is set dressing.
Different jobs, different files — `loadtest.py` generates random bytes with no meaning
and would make a terrible screenshot.

The passwords are printed. They exist only on a local stack seeded by this script.
"""

import argparse
import asyncio
import random
import smtplib
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid

import httpx
from sqlalchemy import delete, func, select

from nimbus import db, storage
from nimbus.api import addresses, security
from nimbus.config import settings
from nimbus.models import (
    Blob,
    BlobChunk,
    Chunk,
    Domain,
    Mailbox,
    MailboxMessage,
    Message,
    Reseller,
)

RESELLER = "demo-seed"
DOMAIN = "acme.demo"
# Six people, because a two-person inbox does not exercise a sender column and a
# fifty-person one is just noise in a screenshot.
STAFF = ["sujal", "priya", "arjun", "meera", "rahul", "support"]
SMTP_HOST, SMTP_PORT = "127.0.0.1", 2525

# Outside senders. Real-looking, with the long display names that break naive layouts.
OUTSIDE = [
    ("Ananya Krishnan", "ananya.krishnan@globex-industries.com"),
    ("David Osei", "d.osei@northwind-logistics.co.uk"),
    ("Yuki Tanaka", "y.tanaka@sakura-manufacturing.jp"),
    ("Procurement (Do Not Reply)", "procurement-noreply@vertex-supplychain.com"),
    ("Fatima Al-Rashid", "f.alrashid@meridian-consulting.ae"),
]

# (filename, content type, size). Sizes are small on purpose — the point is the UI, and
# a 25 MB attachment costs 30 seconds of seeding to look identical on screen.
ATTACHMENTS = [
    ("Q3-board-deck.pdf", "application/pdf", 1_400_000),
    ("supplier-agreement-v4-FINAL-signed.pdf", "application/pdf", 320_000),
    ("headcount-forecast.xlsx", "application/vnd.ms-excel", 88_000),
    ("warehouse-floorplan.png", "image/png", 640_000),
    ("invoice-2026-08-1174.pdf", "application/pdf", 41_000),
]

BODIES = [
    ("Q3 board deck — final before Friday",
     "Attaching the final version. The revenue slide changed after yesterday's call, "
     "everything else is as reviewed.\n\nCould you look at slide 14 specifically? The "
     "margin figure there is the one the board will ask about.",
     True),
    ("Re: warehouse lease — landlord came back",
     "They will do five years at the number we discussed, but they want the break "
     "clause moved to year three instead of year two.\n\nMy read is that this is "
     "acceptable. Thoughts before I reply?",
     False),
    ("Invoice 2026-08-1174 is overdue",
     "This is an automated reminder. Invoice 2026-08-1174 was due on 4 August and "
     "remains unpaid.\n\nIf payment has already been sent, please ignore this message.",
     True),
    ("headcount forecast — needs your numbers by Thursday",
     "I have filled in engineering and sales. Ops and support are still blank and I "
     "cannot close the model without them.",
     False),
    ("Shipment NW-88213 delayed at customs",
     "The container is held pending a certificate of origin. Our broker expects "
     "release within 48 hours but I would not plan around it.",
     False),
    ("Re: supplier agreement — legal signed off",
     "Legal are happy. Signed copy attached. One note: clause 7.2 now says 30 days "
     "rather than 45, which is better for us but worth flagging to finance.",
     True),
    ("Floorplan for the new warehouse",
     "First draft attached. The loading bays are on the north side, which I know is "
     "not what we discussed, but the access road makes the south side impractical.",
     False),
    ("Quick question about the API rate limits",
     "We are seeing 429s at around 200 requests a minute. Is that the documented "
     "ceiling or are we hitting something else?",
     False),
]

HTML_WRAPPER = """<html><body style="font-family:Georgia,serif;color:#2b2b2b">
<p>{body}</p>
<hr style="border:none;border-top:1px solid #ddd">
<p style="font-size:12px;color:#888">{name}<br>{email}<br>
This message and any attachments are confidential.</p>
</body></html>"""


def _attachment(rng: random.Random, spec) -> tuple[str, str, bytes]:
    name, ctype, size = spec
    # Deterministic per filename, so the SAME document sent to several people is
    # byte-identical and the storage screen has real dedup to show. That is the entire
    # reason this is seeded from the name rather than randomly.
    return name, ctype, random.Random(hash(name) & 0xFFFF).randbytes(size)


def _build(rng, sender, to_addrs, subject, body, html, when, attach=None, reply_to=None):
    msg = EmailMessage()
    name, email = sender
    msg["From"] = f"{name} <{email}>"
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg["Date"] = format_datetime(when)
    msg["Message-ID"] = make_msgid(domain="globex-industries.com")
    if reply_to:
        msg["In-Reply-To"] = reply_to
        msg["References"] = reply_to
    msg.set_content(body)
    if html:
        msg.add_alternative(HTML_WRAPPER.format(body=body.replace("\n\n", "</p><p>"),
                                                name=name, email=email), subtype="html")
    if attach:
        fname, ctype, data = attach
        maintype, subtype = ctype.split("/", 1)
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=fname)
    return msg


def _send(msg, to_addrs) -> None:
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=60) as s:
        # A partial refusal is the RETURN value, not an exception — block J rule 4.
        refused = s.send_message(msg, to_addrs=to_addrs)
        if refused:
            raise SystemExit(f"receiver refused {refused} — is the domain verified?")


async def _wait_for(reseller_id, want: int, timeout=120) -> int:
    """Poll until the worker has stored `want` messages. Replies need their parent
    stored first, or the In-Reply-To lookup misses and the thread silently splits."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        async with db.Session() as s:
            n = await s.scalar(
                select(func.count()).select_from(Message).where(Message.reseller_id == reseller_id)
            )
        if n >= want or asyncio.get_running_loop().time() > deadline:
            return n
        await asyncio.sleep(0.5)


async def seed(count: int) -> None:
    rng = random.Random(11)
    key, key_hash = security.new_api_key()

    async with db.Session() as s:
        if await s.scalar(select(Reseller.id).where(Reseller.name == RESELLER)):
            raise SystemExit(f"'{RESELLER}' already exists — run with --clean first")
        s.add(Reseller(name=RESELLER, api_key_hash=key_hash))
        await s.commit()
        rid = await s.scalar(select(Reseller.id).where(Reseller.name == RESELLER))

    passwords = {}
    async with httpx.AsyncClient(timeout=60) as c:
        r = await c.post(
            f"{settings.api_base_url}/v1/orders",
            json={"domain": DOMAIN, "mailboxes": STAFF, "plan": "5GB"},
            headers={"Authorization": f"Bearer {key}", "Idempotency-Key": f"seed-{uuid.uuid4()}"},
        )
        if r.status_code != 201:
            raise SystemExit(f"provisioning failed: {r.status_code} {r.text}")
        for m in r.json()["mailboxes"]:
            passwords[m["address"]] = m["temp_password"]

    # `.demo` has no DNS zone, so the real TXT check can never pass (§9.6a). Same
    # escape hatch the integration tests use.
    async with db.Session() as s:
        d = await s.scalar(select(Domain).where(Domain.name == DOMAIN))
        d.verified = True
        await s.commit()
        await db.connect_redis()
        await addresses.publish_domain(s, d.id)

    now = datetime.now(timezone.utc)
    roots = []

    # ---- Pass 1: root messages. Sent first and waited for, because a reply whose
    # parent is not yet stored starts a new thread instead of joining one.
    for i in range(count):
        subject, body, wants_attachment = BODIES[i % len(BODIES)]
        sender = OUTSIDE[i % len(OUTSIDE)]
        when = now - timedelta(days=rng.randint(0, 12), hours=rng.randint(0, 23))

        # Most mail goes to one person. Some goes to several — which is what makes the
        # storage screen interesting, because the attachment is then stored once.
        n_to = 1 if rng.random() < 0.6 else rng.randint(2, 4)
        to = [f"{p}@{DOMAIN}" for p in rng.sample(STAFF, n_to)]

        attach = _attachment(rng, ATTACHMENTS[i % len(ATTACHMENTS)]) if wants_attachment else None
        msg = _build(rng, sender, to, subject, body, i % 3 != 0, when, attach)
        _send(msg, to)
        roots.append((msg["Message-ID"], sender, to, subject, when))

    stored = await _wait_for(rid, count)
    print(f"  {stored}/{count} root messages stored")

    # ---- Pass 2: replies, so /threads has something real to show.
    replies = 0
    for mid, sender, to, subject, when in roots[: max(2, count // 3)]:
        body = ("Thanks — looked at this. Two things:\n\n"
                "1. The number on slide 14 matches what finance sent me.\n"
                "2. I would push back on the break clause.\n\n"
                "Happy to jump on a call if easier.")
        reply = _build(rng, sender, to,
                       subject if subject.startswith("Re:") else f"Re: {subject}",
                       body, True, when + timedelta(hours=rng.randint(2, 30)),
                       reply_to=mid)
        _send(reply, to)
        replies += 1

    total = await _wait_for(rid, count + replies)
    print(f"  {total}/{count + replies} total stored ({replies} replies)")

    async with db.Session() as s:
        blobs = await s.scalar(
            select(func.count()).select_from(Blob).where(Blob.reseller_id == rid)
        )
        copies = await s.scalar(
            select(func.count()).select_from(MailboxMessage)
            .join(Message, Message.id == MailboxMessage.message_id)
            .where(Message.reseller_id == rid)
        )

    print(f"\n  reseller_id : {rid}")
    print(f"  api_key     : {key}")
    print(f"  domain      : {DOMAIN}   ({blobs} blobs, {copies} inbox copies)")
    print("\n  log in at http://localhost:5173 with any of these:\n")
    for addr, pw in passwords.items():
        print(f"    {addr:24} {pw}")
    await db.close()


async def clean() -> None:
    """Remove everything this script created, including its S3 objects."""
    async with db.Session() as s:
        rid = await s.scalar(select(Reseller.id).where(Reseller.name == RESELLER))
        if rid is None:
            print(f"nothing to clean — no reseller named {RESELLER}")
            await db.close()
            return

        keys = list((await s.scalars(select(Chunk.s3_key).where(Chunk.reseller_id == rid))).all())
        raw = list((await s.scalars(select(Message.raw_s3_key).where(Message.reseller_id == rid))).all())

        # Order matters: mailbox_message first so the refcount trigger fires, then the
        # rows the RESTRICT foreign keys guard. Same staging as HLD §9.8.
        await s.execute(delete(MailboxMessage).where(
            MailboxMessage.message_id.in_(select(Message.id).where(Message.reseller_id == rid))
        ))
        await s.execute(delete(Message).where(Message.reseller_id == rid))
        await s.execute(delete(BlobChunk).where(BlobChunk.reseller_id == rid))
        await s.execute(delete(Blob).where(Blob.reseller_id == rid))
        await s.execute(delete(Chunk).where(Chunk.reseller_id == rid))
        await s.execute(delete(Mailbox).where(
            Mailbox.domain_id.in_(select(Domain.id).where(Domain.reseller_id == rid))
        ))
        await s.execute(delete(Domain).where(Domain.reseller_id == rid))
        await s.execute(delete(Reseller).where(Reseller.id == rid))
        await s.commit()

        await db.connect_redis()
        await addresses.refresh(s)

    # `delete_chunks` batches and is already async — it deletes any key, not only
    # chunks, so the raw .eml objects go through it too rather than growing a second
    # deleter that would need its own error handling.
    doomed = keys + [k for k in raw if k]
    if doomed:
        refused = await storage.delete_chunks(doomed)
        if refused:
            print(f"  S3 refused {len(refused)} key(s); they are stranded: {refused[:3]}")

    print(f"cleaned {RESELLER}: {len(keys)} chunks, {len(raw)} raw messages")
    await db.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--clean", action="store_true", help="remove the seeded data")
    ap.add_argument("--messages", type=int, default=24)
    args = ap.parse_args()
    asyncio.run(clean() if args.clean else seed(args.messages))
