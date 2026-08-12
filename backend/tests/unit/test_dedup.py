"""The one runnable check for block C. No database, no S3, no Kafka.

    cd backend/worker
    uv run python test_dedup.py

`dedup.chunk_and_hash` is a pure function, and everything else in the block is built on
top of its output — S3 keys, blob rows, the chunk map, refcounts. If the arithmetic here
is wrong, every one of those is wrong in a way nothing downstream can detect: Postgres
cannot tell that a chunk map is internally consistent but reassembles into garbage.

So this is the cheapest test that still fails the moment the dedup math breaks.
"""

import io

from nimbus.worker import dedup
from nimbus.worker import mime


def test_round_trip():
    """The invariant that matters most: the pieces put back together are the original.

    A bug here silently corrupts every stored attachment. Nothing else in the system
    would notice — the rows would all be consistent with each other and wrong.
    """
    for size in (0, 1, 100, dedup.CHUNK_SIZE - 1, dedup.CHUNK_SIZE, dedup.CHUNK_SIZE + 1):
        data = bytes(range(256)) * (size // 256) + bytes(range(size % 256))
        data = data[:size]
        _, chunks = dedup.chunk_and_hash(data)
        assert b"".join(c for _, c in chunks) == data, f"round trip failed at {size} bytes"


def test_chunk_boundaries():
    data = b"x" * (dedup.CHUNK_SIZE * 2 + 17)
    _, chunks = dedup.chunk_and_hash(data)
    assert len(chunks) == 3, f"expected 3 chunks, got {len(chunks)}"
    assert len(chunks[0][1]) == dedup.CHUNK_SIZE
    assert len(chunks[1][1]) == dedup.CHUNK_SIZE
    assert len(chunks[2][1]) == 17, "the last chunk keeps the remainder, not padding"


def test_blob_hash_ignores_chunk_size():
    """The blob hash must be of the FILE, not of the chunk layout.

    Hashing the chunk hashes together would look almost right and be quietly wrong: the
    same file chunked differently would get a different blob hash, so dedup would miss
    every duplicate the moment the chunk size ever changed.
    """
    data = b"the same bytes either way" * 1000
    small, _ = dedup.chunk_and_hash(data, chunk_size=64)
    large, _ = dedup.chunk_and_hash(data, chunk_size=8192)
    assert small == large, "blob hash depends on chunk size"


def test_identical_content_is_identical_everywhere():
    """This IS the dedup guarantee. Two senders, same file, same addresses."""
    data = b"quarterly-report" * 5000
    first_hash, first_chunks = dedup.chunk_and_hash(data)
    second_hash, second_chunks = dedup.chunk_and_hash(bytes(data))
    assert first_hash == second_hash
    assert [h for h, _ in first_chunks] == [h for h, _ in second_chunks]


def test_one_flipped_byte_changes_the_hash():
    data = bytearray(b"a" * 10_000)
    before, _ = dedup.chunk_and_hash(bytes(data))
    data[5000] = ord("b")
    after, _ = dedup.chunk_and_hash(bytes(data))
    assert before != after, "a changed file kept the same fingerprint"


def test_hash_is_the_right_shape():
    """CHAR(64) in the schema. A different width would fail on insert, not here."""
    blob_hash, chunks = dedup.chunk_and_hash(b"anything")
    assert len(blob_hash) == 64 and all(len(h) == 64 for h, _ in chunks)


def test_chunk_key_is_reseller_scoped():
    """Per-reseller dedup is a security boundary, not a storage detail (HLD §12).

    A shared key would put two customers' identical files at the same S3 address, which
    is the cross-tenant leak the composite primary key exists to prevent.
    """
    h = "a" * 64
    assert dedup.chunk_s3_key("reseller-one", h) != dedup.chunk_s3_key("reseller-two", h)
    assert dedup.chunk_s3_key("reseller-one", h).startswith("reseller-one/")


def test_empty_attachment():
    """Legal in MIME. Must produce a hash and no chunks, not a crash."""
    blob_hash, chunks = dedup.chunk_and_hash(b"")
    assert chunks == []
    assert len(blob_hash) == 64


def test_nul_bytes_are_stripped_from_sender_supplied_strings():
    """Postgres TEXT cannot hold 0x00, and every one of these comes from the sender.

    Without the strip, `Subject: a\\x00b` makes asyncpg raise, the transaction rolls
    back, the Kafka offset is never committed, and the worker redelivers that same
    message forever — one email from anyone on the internet halts a partition.
    """
    raw = (
        b"From: ce\x00o@globex.com\r\nSubject: q\x003 deck\r\n"
        b"Message-ID: <a\x00b@globex.com>\r\nIn-Reply-To: <c\x00d@globex.com>\r\n"
        b"MIME-Version: 1.0\r\nContent-Type: multipart/mixed; boundary=B\r\n\r\n"
        b"--B\r\nContent-Type: text/plain\r\n\r\nhi\r\n"
        b"--B\r\nContent-Type: application/pdf\r\n"
        b'Content-Disposition: attachment; filename="dec\x00k.pdf"\r\n\r\nzz\r\n--B--\r\n'
    )
    message = mime.parse(io.BytesIO(raw))
    headers = mime.headers(message)
    values = [headers.subject, headers.from_addr, headers.message_id, headers.in_reply_to]
    values += [a.filename for a in mime.attachments(message)]
    values += [a.content_type for a in mime.attachments(message)]
    for value in values:
        assert value is not None, "the probe stopped exercising a field"
        assert "\x00" not in value, f"NUL survived into {value!r} — Postgres will reject it"
