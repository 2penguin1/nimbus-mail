"""HTTP range requests, mapped onto a chunked attachment. Pure arithmetic, no I/O.

An attachment is stored as an ordered list of 4 MB chunks (HLD §9.3). A browser asking
for `Range: bytes=5000000-9000000` wants a window that starts inside chunk 1 and ends
inside chunk 2. Working out which chunks, and which slice of each, is fiddly enough to
get wrong and easy to test — so it lives here, apart from the HTTP and the S3 calls.

Getting it wrong does not raise. It silently returns the wrong bytes, and a video that
seeks to the middle plays garbage.
"""


class Unsatisfiable(ValueError):
    """The range asks for bytes beyond the end of the file. Answer 416."""


def parse(header: str | None, total: int) -> tuple[int, int] | None:
    """Read a `Range:` header. Returns inclusive (start, end), or None for "send it all".

    Handles the three forms RFC 7233 defines for a single range:

        bytes=0-499     the first 500 bytes
        bytes=500-      from 500 to the end
        bytes=-500      the LAST 500 bytes

    Anything malformed returns None rather than failing. RFC 7233 §3.1 says an
    unparseable Range must be ignored and the whole thing sent — a broken header should
    degrade to a working download, not a broken one.

    Multiple ranges (`bytes=0-99,200-299`) are deliberately not supported: they require
    a multipart/byteranges response, and no mail client asks for them. We serve the
    whole file instead, which is a legal answer to any range request.
    """
    if not header or not header.startswith("bytes=") or total <= 0:
        return None

    spec = header[len("bytes="):].strip()
    if "," in spec or "-" not in spec:
        return None

    first, _, last = spec.partition("-")
    try:
        if not first:
            # bytes=-N — the final N bytes. N greater than the file means the whole file.
            suffix = int(last)
            if suffix <= 0:
                return None
            return max(0, total - suffix), total - 1
        start = int(first)
        end = int(last) if last else total - 1
    except ValueError:
        return None

    if start < 0:
        return None
    # This check MUST come before the backwards-range check below. `bytes=<total>-`
    # expands to start=total, end=total-1, which looks backwards — and returning None
    # there would quietly send the whole file to a client that asked to resume past the
    # end. It needs to be told its length is wrong, not handed 10 MB it did not ask for.
    if start >= total:
        raise Unsatisfiable(f"range starts at {start}, file is {total} bytes")
    if end < start:
        return None
    return start, min(end, total - 1)


def slice_chunks(sizes: list[int], start: int, end: int) -> list[tuple[int, int, int]]:
    """Which chunks cover bytes [start, end], and what part of each.

    Returns (chunk_index, offset_within_chunk, length) per chunk touched, in order.
    The caller reads exactly those slices and concatenates them — so a request for 1 KB
    out of a 40 MB attachment moves 1 KB, not 40 MB.

    Both bounds are INCLUSIVE, matching how HTTP ranges are defined. Off-by-one here is
    the classic way to lose the last byte of every download.
    """
    plan: list[tuple[int, int, int]] = []
    position = 0
    for index, size in enumerate(sizes):
        chunk_first = position
        chunk_last = position + size - 1
        position += size

        if chunk_last < start:
            continue  # entirely before the window
        if chunk_first > end:
            break  # entirely after it, and every later chunk is too

        offset = max(0, start - chunk_first)
        last_wanted = min(size - 1, end - chunk_first)
        plan.append((index, offset, last_wanted - offset + 1))
    return plan
