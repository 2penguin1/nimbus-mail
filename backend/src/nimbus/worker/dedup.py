"""Content addressing — the arithmetic the whole project is built on.

Deliberately pure: bytes in, hashes out. No database, no S3, no Kafka, no config. That
is what makes the one thing that must never be wrong testable in milliseconds with no
infrastructure at all (see test_dedup.py).

Two hashes, and the difference matters:

* the **blob hash** is SHA-256 of the WHOLE attachment. It answers "have we stored this
  exact file before?"
* a **chunk hash** is SHA-256 of one 4 MB slice. It answers "have we stored this exact
  piece before?", which is what lets two files that share most of their content share
  the stored bytes.

Hashing the chunk hashes together instead of the original bytes would look almost right
and be quietly wrong: the blob hash would then depend on where the chunk boundaries fell.
"""

import hashlib

# ponytail: fixed 4 MB slices. Fixed chunking only dedups files that are byte-identical
# from the start — insert one byte at the front and every boundary shifts, so nothing
# matches. Content-defined (rolling-hash) chunking fixes that and is the upgrade path if
# the measured dedup ratio in HLD §13 disappoints. Mail attachments are near-always
# re-sent unmodified, so fixed is the right first answer.
CHUNK_SIZE = 4 * 1024 * 1024


def sha256(data: bytes) -> str:
    """64 lowercase hex characters — exactly the width of the CHAR(64) columns."""
    return hashlib.sha256(data).hexdigest()


def chunk_and_hash(
    data: bytes, chunk_size: int = CHUNK_SIZE
) -> tuple[str, list[tuple[str, bytes]]]:
    """Split one attachment into addressable pieces.

    Returns (blob_hash, [(chunk_hash, chunk_bytes), ...]) with the chunks in order.
    That order IS the chunk map — its index becomes `blob_chunk.seq`, and getting it
    wrong reassembles the file into garbage that nothing downstream can detect.

    A zero-byte attachment is legal in MIME and yields no chunks at all, so the blob
    lands with chunk_count = 0 and reassembles to b"". That is correct, not a gap.
    """
    chunks = [data[i:i + chunk_size] for i in range(0, len(data), chunk_size)]
    return sha256(data), [(sha256(c), c) for c in chunks]


def chunk_s3_key(reseller_id, chunk_hash: str) -> str:
    """Where a chunk's bytes live.

    Two properties are load-bearing:

    * **reseller_id is in the path.** Dedup is scoped per reseller (HLD §12), so the
      same bytes belonging to two customers are two separate objects. A shared path
      would put the cross-tenant leak back that the composite primary key removes.
    * **The key is derived from the content, never random.** So a crash between the
      upload and the database commit leaves the object at exactly the address the retry
      will write to — the retry overwrites it with identical bytes and moves on. Nothing
      to sweep, nothing orphaned. The receiver's raw .eml keys are random and genuinely
      can orphan; these cannot.

    The two-character prefix keeps any single directory listing small on filesystems and
    spreads load across S3 partitions.
    """
    return f"{reseller_id}/{chunk_hash[:2]}/{chunk_hash}.bin"
