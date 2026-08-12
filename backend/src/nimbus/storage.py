"""S3 access, shared by every service — one boto3 client for MinIO and real S3.

`endpoint_url` is the whole trick: point it at MinIO in development, leave it at AWS in
production, and not a line of this code changes. That is the cloud-agnostic promise in
HLD §14.

Lives at the package root rather than under `worker/` because the API reads chunks back
out (block E) and the worker writes them in (block C). Two clients would mean two sets
of credentials and two timeout policies drifting apart.

boto3 is synchronous. Rather than add an async S3 library, calls go through
`asyncio.to_thread` — stdlib, and the overhead is irrelevant next to a network round
trip. Without it, one transfer would block the event loop and stall every other request.
"""

import asyncio
import logging

import boto3
from botocore.config import Config

from nimbus.config import settings

log = logging.getLogger("nimbus.storage")

# Timeouts are not tuning. boto3's defaults are a 60-second connect, a 60-second read and
# the LEGACY retry mode — up to 5 attempts — which multiplies out to roughly 600 seconds
# before a call gives up.
#
# That number matters because of where the slowest caller sits. `gc.py` phase 3 deletes
# S3 objects INSIDE its transaction, deliberately (committing first leaves a window where
# the worker re-uploads the bytes and our delete lands after it). So it is holding
# `FOR UPDATE` on up to `BATCH` chunk rows while it waits. Every worker storing an
# attachment that shares one of those chunks needs `FOR KEY SHARE` on the same rows and
# blocks behind it. One MinIO or S3 hiccup would therefore stall mail delivery for ten
# minutes, and nothing in the logs would connect the two.
#
# 30 seconds is still generous for a 4 MB chunk — `read_timeout` applies per socket read,
# not to a whole transfer, so streaming a 40 MB `.eml` is unaffected. `standard` retry
# mode also stops retrying the errors that are not worth retrying.
client = boto3.client(
    "s3",
    endpoint_url=settings.s3_endpoint,
    aws_access_key_id=settings.s3_access_key,
    aws_secret_access_key=settings.s3_secret_key,
    region_name=settings.s3_region,
    config=Config(
        connect_timeout=5,
        read_timeout=30,
        retries={"max_attempts": 3, "mode": "standard"},
    ),
)


def open_raw_message(s3_key: str):
    """BLOCKING. Open the raw .eml the receiver stored, as a readable stream.

    Deliberately synchronous, and callers must run it inside `asyncio.to_thread` along
    with the parse that consumes it. Wrapping only `get_object` would be a trap: the
    call returns as soon as the HTTP headers arrive, and every byte of the 34 MB body is
    then pulled on whatever thread reads the stream. Offloading the open but parsing on
    the event loop blocks it for the whole download plus the parse — measured at 627 ms
    for one 25 MB attachment, which is long enough for aiokafka's heartbeat to miss its
    3-second interval and get this worker evicted from the consumer group.
    """
    return client.get_object(Bucket=settings.s3_raw_bucket, Key=s3_key)["Body"]


async def put_chunk(key: str, data: bytes) -> None:
    """Store one chunk.

    Overwriting is expected and harmless: the key is derived from a SHA-256 of exactly
    these bytes, so anything already at that address is byte-identical to what we are
    writing. That is what makes a crashed-and-retried upload self-healing.
    """
    await asyncio.to_thread(
        client.put_object, Bucket=settings.s3_chunk_bucket, Key=key, Body=data
    )


async def delete_chunks(keys: list[str]) -> list[str]:
    """Delete chunk objects. Returns the keys S3 refused, which is normally empty.

    One batched request rather than one per key: a sweep freeing 500 chunks should be one
    round trip, not 500.

    **Deleting a key that is not there is a success, not an error.** That is what makes
    the sweep safely repeatable — a crash between this call and the commit rolls the rows
    back, and the next sweep asks S3 to delete objects that are already gone.

    boto3 does NOT raise for a per-key failure; it returns them in `Errors` and the call
    still succeeds overall. Silently ignoring that would leave objects nobody has a row
    for — invisible, and billed for ever — so the caller gets them back and logs them.
    """
    if not keys:
        return []

    def send() -> list[str]:
        failed: list[str] = []
        # S3 caps DeleteObjects at 1000 keys. GC batches at 500 so this never binds, but
        # the slice keeps that a local fact rather than a promise made somewhere else.
        for start in range(0, len(keys), 1000):
            response = client.delete_objects(
                Bucket=settings.s3_chunk_bucket,
                Delete={"Objects": [{"Key": k} for k in keys[start:start + 1000]]},
            )
            failed.extend(error["Key"] for error in response.get("Errors", []))
        return failed

    return await asyncio.to_thread(send)


async def get_chunk(key: str, offset: int = 0, length: int | None = None) -> bytes:
    """Read a chunk, or a slice of one.

    The slice is pushed down to S3 with its own `Range` header rather than fetched whole
    and cut here. Serving a 1 KB browser range request out of a 4 MB chunk should move
    1 KB across the network, not 4 MB — and this is the difference between a video
    seeking smoothly and re-downloading the file on every scrub.
    """
    extra = {}
    if offset or length is not None:
        end = "" if length is None else offset + length - 1
        extra["Range"] = f"bytes={offset}-{end}"
    # One hop, not two. Fetching and reading in separate to_thread calls costs two
    # thread-pool round trips per chunk — twenty for a 40 MB download, all on the hot path.
    def fetch() -> bytes:
        response = client.get_object(Bucket=settings.s3_chunk_bucket, Key=key, **extra)
        return response["Body"].read()

    return await asyncio.to_thread(fetch)
