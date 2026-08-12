"""The block C proof: send real mail, show the second copy stores nothing.

Needs the whole stack running — Docker, the API, the SMTP receiver, and the worker:

    docker compose -f infra/docker-compose.yml up -d
    cd backend/api    && uv run python -m uvicorn main:app     # terminal 1
    cd backend/smtp-receiver && go run .                       # terminal 2
    cd backend/worker && uv run python main.py                 # terminal 3
    cd backend/worker && uv run python test_dedup_live.py      # terminal 4

What it does: provisions two mailboxes at one reseller, then sends the SAME 10 MB
attachment in two separate emails. A naive mail server stores 20 MB. We must store 10.

The number this prints is the entire business case of the project (HLD §2).
"""

import asyncio
import secrets
import smtplib
import time
from email.message import EmailMessage

import boto3
import httpx
import pytest
from sqlalchemy import delete, func, select

from nimbus import db
from nimbus.api import security
from nimbus.config import settings
from nimbus.worker import dedup
from nimbus.models import (
    Blob,
    BlobChunk,
    Chunk,
    Domain,
    Message,
    MessageAttachment,
    ProcessedEvent,
    Reseller,
)


# Needs the whole stack: Docker, the API, the receiver AND the worker.
pytestmark = pytest.mark.integration

TAG = secrets.token_hex(4)
DOMAIN = f"dedup-{TAG}.example"
ATTACHMENT_MB = 10
_WANT = ATTACHMENT_MB * 1024 * 1024
_FILLER = b"NIMBUS-DEDUP-PAYLOAD-"
# Deterministic, not random: random bytes would still dedup, but this makes a failure
# reproducible and the chunk hashes stable across runs of the same size.
#
# Built from the target size, never a guessed repeat count. Writing a fixed multiplier
# and slicing to _WANT silently produces a SHORTER payload whenever the multiplier is
# too small — the test then passes while proving nothing about multi-chunk files. The
# assert is here because that exact mistake already happened once.
PAYLOAD = (_FILLER * (_WANT // len(_FILLER) + 1))[:_WANT]
assert len(PAYLOAD) == _WANT, f"payload is {len(PAYLOAD)} bytes, wanted {_WANT}"
EXPECTED_CHUNKS = -(-len(PAYLOAD) // dedup.CHUNK_SIZE)  # ceiling division
assert EXPECTED_CHUNKS > 1, "the point is to exercise a MULTI-chunk attachment"


def send(to_addrs: list[str], subject: str) -> None:
    msg = EmailMessage()
    msg["From"] = "ceo@globex.com"
    msg["To"] = ", ".join(to_addrs)
    msg["Subject"] = subject
    msg.set_content("Please review the attached deck.")
    msg.add_attachment(
        PAYLOAD, maintype="application", subtype="pdf", filename="pitch-deck.pdf"
    )
    with smtplib.SMTP("127.0.0.1", 2525, timeout=120) as s:
        s.send_message(msg, from_addr="ceo@globex.com", to_addrs=to_addrs)


async def wait_for(session, reseller_id, want: int, seconds: int = 90) -> int:
    """Poll until the worker has stored `want` messages. It runs asynchronously."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        session.expire_all()
        got = await session.scalar(
            select(func.count()).select_from(Message).where(Message.reseller_id == reseller_id)
        )
        if got >= want:
            return got
        await asyncio.sleep(2)
    return got


async def test_dedup_stores_one_copy() -> None:
    s3 = boto3.client(
        "s3",
        endpoint_url=settings.s3_endpoint,
        aws_access_key_id=settings.s3_access_key,
        aws_secret_access_key=settings.s3_secret_key,
        region_name=settings.s3_region,
    )
    async with db.Session() as session:
        key, key_hash = security.new_api_key()
        session.add(Reseller(name=f"dedup-{TAG}", api_key_hash=key_hash))
        await session.commit()
        reseller_id = await session.scalar(
            select(Reseller.id).where(Reseller.name == f"dedup-{TAG}")
        )

        try:
            async with httpx.AsyncClient(timeout=60) as client:
                r = await client.post(
                    f"{settings.api_base_url}/v1/orders",
                    json={"domain": DOMAIN, "mailboxes": ["alice", "bob"]},
                    headers={"Authorization": f"Bearer {key}", "Idempotency-Key": TAG},
                )
                assert r.status_code == 201, r.text
            print(f"ok   provisioned alice@{DOMAIN} and bob@{DOMAIN}")

            # One email, TWO recipients at the same reseller -> one message row.
            send([f"alice@{DOMAIN}", f"bob@{DOMAIN}"], "Q3 deck")
            # A DIFFERENT email carrying the SAME attachment -> the dedup hit.
            send([f"alice@{DOMAIN}"], "Q3 deck, resend")
            print(f"ok   sent 2 emails carrying the same {ATTACHMENT_MB} MB attachment")

            stored = await wait_for(session, reseller_id, want=2)
            assert stored == 2, f"worker stored {stored} messages, expected 2"
            print("ok   worker processed both")

            blobs = await session.scalar(
                select(func.count()).select_from(Blob).where(Blob.reseller_id == reseller_id)
            )
            assert blobs == 1, f"expected 1 blob, got {blobs} — dedup did not happen"
            print(f"ok   1 blob row for 2 deliveries of the same file")

            chunks = (
                await session.scalars(
                    select(Chunk).where(Chunk.reseller_id == reseller_id)
                )
            ).all()
            assert len(chunks) == EXPECTED_CHUNKS, \
                f"expected {EXPECTED_CHUNKS} chunks, got {len(chunks)}"
            print(f"ok   {len(chunks)} chunks of {dedup.CHUNK_SIZE // 1024 // 1024} MB")

            # The bug this catches: `if created:` instead of `if created is not None:`
            # skips seq 0, leaving chunk 0 of every attachment at refcount 0 for GC.
            for chunk in chunks:
                assert chunk.refcount == 1, \
                    f"chunk {chunk.hash[:8]} has refcount {chunk.refcount}, expected 1"
            print("ok   every chunk refcount == 1, including seq 0")

            maps = await session.scalar(
                select(func.count()).select_from(BlobChunk)
                .where(BlobChunk.reseller_id == reseller_id)
            )
            assert maps == EXPECTED_CHUNKS, f"chunk map has {maps} rows"

            links = await session.scalar(
                select(func.count()).select_from(MessageAttachment)
                .where(MessageAttachment.reseller_id == reseller_id)
            )
            assert links == 2, f"expected 2 message_attachment rows, got {links}"
            print("ok   2 messages both point at the one blob")

            objects = s3.list_objects_v2(
                Bucket=settings.s3_chunk_bucket, Prefix=f"{reseller_id}/"
            )
            n_objects = objects.get("KeyCount", 0)
            physical = sum(o["Size"] for o in objects.get("Contents", []))
            assert n_objects == EXPECTED_CHUNKS, \
                f"S3 holds {n_objects} objects, expected {EXPECTED_CHUNKS}"
            print(f"ok   S3 holds exactly {n_objects} objects")

            logical = len(PAYLOAD) * 2  # what the two mailboxes think they received
            saved = 100 * (1 - physical / logical)
            print(f"\n   logical  {logical / 1024 / 1024:6.1f} MB   (what the users see)")
            print(f"   physical {physical / 1024 / 1024:6.1f} MB   (what we actually stored)")
            print(f"   saved    {saved:6.1f} %\n")
            assert physical == len(PAYLOAD), "stored bytes != one copy of the file"

            print("9 checks passed")
        finally:
            # RESTRICT on reseller_id means blobs, chunks and messages must go first —
            # exactly the staged delete DATABASE.md §9 Trap 1 describes.
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
            for page in s3.get_paginator("list_objects_v2").paginate(
                Bucket=settings.s3_chunk_bucket, Prefix=f"{reseller_id}/"
            ):
                for obj in page.get("Contents", []):
                    s3.delete_object(Bucket=settings.s3_chunk_bucket, Key=obj["Key"])
    await db.close()
