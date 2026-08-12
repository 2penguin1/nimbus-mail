"""HTTP range arithmetic. No database, no S3, no server.

This is the one part of block E that is pure logic, and it is the part that fails
silently: a bad range does not raise, it serves the wrong bytes. A video seeks to the
middle and plays garbage, a resumed download finishes corrupt, and nothing in the system
notices. So it gets the check.
"""

import pytest

from nimbus.api import ranges

MB = 1024 * 1024
# A 10 MB attachment as the worker actually stores it: 4 + 4 + 2.
CHUNKS = [4 * MB, 4 * MB, 2 * MB]
TOTAL = sum(CHUNKS)


# --------------------------------------------------------------------------
# parse
# --------------------------------------------------------------------------

def test_no_header_means_send_everything():
    assert ranges.parse(None, TOTAL) is None
    assert ranges.parse("", TOTAL) is None


def test_explicit_range():
    assert ranges.parse("bytes=0-499", TOTAL) == (0, 499)
    assert ranges.parse("bytes=100-200", TOTAL) == (100, 200)


def test_open_ended_range_runs_to_the_end():
    """`bytes=500-` is what a resumed download sends."""
    assert ranges.parse("bytes=500-", TOTAL) == (500, TOTAL - 1)


def test_suffix_range_takes_the_last_n_bytes():
    """`bytes=-500` means the LAST 500, not the first 500. Easy to invert."""
    assert ranges.parse("bytes=-500", TOTAL) == (TOTAL - 500, TOTAL - 1)


def test_suffix_longer_than_the_file_is_the_whole_file():
    assert ranges.parse(f"bytes=-{TOTAL * 2}", TOTAL) == (0, TOTAL - 1)


def test_end_past_the_file_is_clamped():
    assert ranges.parse(f"bytes=0-{TOTAL * 5}", TOTAL) == (0, TOTAL - 1)


def test_start_past_the_file_is_unsatisfiable():
    """The one case that must NOT quietly succeed — the client has the wrong length."""
    with pytest.raises(ranges.Unsatisfiable):
        ranges.parse(f"bytes={TOTAL}-", TOTAL)


@pytest.mark.parametrize(
    "header",
    [
        "items=0-10",       # not bytes
        "bytes=abc-def",    # not numbers
        "bytes=0-99,200-", # multiple ranges, deliberately unsupported
        "bytes=500-100",    # backwards
        "bytes=",           # empty
        "bytes=-0",         # a zero-length suffix means nothing
    ],
)
def test_malformed_ranges_fall_back_to_the_whole_file(header):
    """RFC 7233 §3.1: ignore a Range you cannot parse. A broken header should degrade
    to a working download, never to a broken one."""
    assert ranges.parse(header, TOTAL) is None


# --------------------------------------------------------------------------
# slice_chunks
# --------------------------------------------------------------------------

def test_whole_file_touches_every_chunk_completely():
    plan = ranges.slice_chunks(CHUNKS, 0, TOTAL - 1)
    assert plan == [(0, 0, 4 * MB), (1, 0, 4 * MB), (2, 0, 2 * MB)]


def test_a_window_inside_one_chunk_touches_only_that_chunk():
    plan = ranges.slice_chunks(CHUNKS, 100, 199)
    assert plan == [(0, 100, 100)], "reading 100 bytes must not fetch 4 MB"


def test_a_window_spanning_two_chunks():
    """The case that motivated this module: start in chunk 0, end in chunk 1."""
    start = 4 * MB - 10
    end = 4 * MB + 9
    plan = ranges.slice_chunks(CHUNKS, start, end)
    assert plan == [(0, 4 * MB - 10, 10), (1, 0, 10)]
    assert sum(length for _, _, length in plan) == end - start + 1


def test_a_window_that_skips_the_first_chunk_entirely():
    plan = ranges.slice_chunks(CHUNKS, 8 * MB, TOTAL - 1)
    assert plan == [(2, 0, 2 * MB)], "chunks before the window must not be fetched"


def test_the_very_last_byte():
    """Off-by-one here loses the final byte of every download."""
    plan = ranges.slice_chunks(CHUNKS, TOTAL - 1, TOTAL - 1)
    assert plan == [(2, 2 * MB - 1, 1)]


def test_the_very_first_byte():
    assert ranges.slice_chunks(CHUNKS, 0, 0) == [(0, 0, 1)]


def test_every_window_reassembles_to_exactly_the_bytes_asked_for():
    """The invariant that matters: the plan covers the window, contiguously, once.

    Rebuilt against a real byte string so a wrong offset shows up as wrong CONTENT,
    not just a wrong length — a plan can have the right total and still be scrambled.
    """
    sizes = [7, 5, 11, 1, 9]
    data = bytes(range(sum(sizes)))
    pieces, at = [], 0
    for size in sizes:
        pieces.append(data[at:at + size])
        at += size

    for start in range(len(data)):
        for end in range(start, len(data)):
            plan = ranges.slice_chunks(sizes, start, end)
            rebuilt = b"".join(
                pieces[i][offset:offset + length] for i, offset, length in plan
            )
            assert rebuilt == data[start:end + 1], f"wrong bytes for {start}-{end}"


def test_a_single_chunk_attachment():
    assert ranges.slice_chunks([100], 10, 20) == [(0, 10, 11)]


def test_an_empty_attachment_has_no_chunks():
    assert ranges.slice_chunks([], 0, 0) == []
