"""What goes into the search index. `build()` is pure, so no database is needed.

The cap tests are the ones that matter. `to_tsvector` raising inside the delivery
transaction would roll back the stored message and its delivery, the Kafka offset would
never commit, and the worker would redeliver that message forever — one email would stop
a partition permanently. So the cap is not a tidiness rule, it is the guard that makes
that unreachable.
"""

from sqlalchemy.exc import DBAPIError, InterfaceError, NotSupportedError

from nimbus.search_index import MAX_INDEXED_BYTES, _is_transient, build


def _failure(sqlstate=None, cls=DBAPIError):
    """A real SQLAlchemy DBAPIError, not a stand-in.

    Built from the actual classes because `_is_transient` now branches on BOTH the
    exception type and `orig.sqlstate`. A duck-typed stub would pass the sqlstate half
    while silently skipping the isinstance half — which is how the missing
    `InvalidCachedStatementError` case survived a review.

    `sqlstate=None` models a client-side driver error that never reached Postgres, so
    there is no SQLSTATE to classify on.
    """
    orig = Exception("boom")
    if sqlstate is not None:
        orig.sqlstate = sqlstate
    return cls("INSERT INTO message_index ...", None, orig)


def test_the_ordinary_case():
    text = build(
        subject="Q3 invoice",
        from_addr="boss@acme.com",
        filenames=["report.pdf"],
        body_text="please review the attached invoice",
        body_html=None,
    )
    assert text.subject == "Q3 invoice"
    assert "boss@acme.com" in text.body
    assert "report.pdf" in text.body, "filenames must be findable — 'that PDF called Q3'"
    assert "please review the attached invoice" in text.body


def test_subject_is_kept_apart_from_everything_else():
    # Two bands, so the subject can outrank the body. If they merged, the weighting in
    # search_index.write would be meaningless.
    text = build("SUBJECT", "sender", [], "BODY", None)
    assert text.subject == "SUBJECT"
    assert "SUBJECT" not in text.body


def test_html_is_used_only_when_there_is_no_plain_text():
    text = build(None, None, [], None, "<div class='x'><p>hello <b>world</b></p></div>")
    assert "hello" in text.body and "world" in text.body
    # Indexing raw markup would make every message in the system match "div".
    assert "div" not in text.body
    assert "class" not in text.body


def test_plain_text_wins_over_html_when_both_exist():
    text = build(None, None, [], "the real body", "<p>the html body</p>")
    assert "the real body" in text.body
    assert "html body" not in text.body


def test_missing_everything_does_not_crash():
    text = build(None, None, [], None, None)
    assert text.subject == ""
    assert text.body.strip() == ""


def test_a_none_filename_is_survivable():
    # An inline image often has no filename at all. mime.Attachment.filename is
    # `str | None` for exactly this reason.
    text = build("s", "f", [None, "real.pdf"], "b", None)
    assert "real.pdf" in text.body


# --------------------------------------------------------------------------
# The cap — measured in BYTES, because that is what Postgres counts
# --------------------------------------------------------------------------

def test_a_huge_body_is_truncated():
    text = build(None, None, [], "x" * (MAX_INDEXED_BYTES * 3), None)
    assert len(text.body.encode()) <= MAX_INDEXED_BYTES


def test_multibyte_text_is_capped_by_BYTES_not_characters():
    # 1 emoji is 4 bytes. Capping on len(str) would let 4x the limit through — which is
    # exactly how mime.MAX_BODY_BYTES (a character cap) fails to protect to_tsvector.
    body = "🙂" * MAX_INDEXED_BYTES
    text = build(None, None, [], body, None)
    encoded = text.body.encode()
    assert len(encoded) <= MAX_INDEXED_BYTES
    # And it must not have sliced a character in half.
    assert encoded.decode() == text.body


def test_nul_bytes_never_reach_postgres():
    # Postgres TEXT cannot hold 0x00. asyncpg raises, the transaction rolls back, and the
    # message redelivers forever — the bug mime._str already fixed once, guarded here
    # from the second direction.
    text = build("a\x00b", "c\x00d", ["e\x00f"], "g\x00h", None)
    assert "\x00" not in text.subject
    assert "\x00" not in text.body


def test_the_subject_is_capped_too():
    # A subject header is attacker-supplied and has no length limit that anyone enforces.
    text = build("s" * (MAX_INDEXED_BYTES * 2), None, [], None, None)
    assert len(text.subject.encode()) <= MAX_INDEXED_BYTES


TSVECTOR_CEILING = 1_048_575  # Postgres's hard limit
# Measured on Postgres 16. NOT the 2.1x that plain distinct words give: a URL-shaped
# token emits THREE lexemes (`url`, `host`, `url_path`), and 130 KB of `<hex>.co/<hex>`
# produced a 473,762-byte tsvector — 3.64x. Rounded up for headroom.
WORST_EXPANSION = 3.8


def test_the_two_bands_together_stay_under_the_tsvector_ceiling():
    """The budget is JOINT — `write()` concatenates the bands with `||`.

    This test exists because the first version of the cap reasoned about one band and was
    wrong by exactly the factor of two the concatenation introduces. Raising
    MAX_INDEXED_BYTES without redoing the arithmetic fails here rather than in production
    on one crafted email.
    """
    text = build(
        "s" * (MAX_INDEXED_BYTES * 2),
        None,
        [],
        "b" * (MAX_INDEXED_BYTES * 2),
        None,
    )
    combined = len(text.subject.encode()) + len(text.body.encode())
    assert combined <= 2 * MAX_INDEXED_BYTES
    assert combined * WORST_EXPANSION < TSVECTOR_CEILING, (
        f"{combined} bytes could expand to {combined * WORST_EXPANSION:.0f}, "
        f"over Postgres's {TSVECTOR_CEILING}-byte tsvector ceiling"
    )


# --------------------------------------------------------------------------
# Which failures keep the mail, and which ones get retried
# --------------------------------------------------------------------------

def test_transient_failures_are_retried_not_swallowed():
    # A deadlock, a dropped connection or a full disk says nothing about this message.
    # Swallowing it would commit the mail unindexed for a reason that would have gone
    # away on the next attempt — and there is no repair path, so "unindexed" is forever.
    for sqlstate in ["40P01", "40001", "08006", "08003", "53100", "53200", "57014"]:
        assert _is_transient(_failure(sqlstate)) is True, sqlstate


def test_data_failures_are_swallowed_so_the_mail_survives():
    # 54000 is what a tsvector overflow actually raises — verified against Postgres 16.
    # No retry can fix these, so the mail must be kept and the index row lost.
    for sqlstate in ["54000", "22021", "42883", "23505"]:
        assert _is_transient(_failure(sqlstate)) is False, sqlstate


def test_a_driver_error_with_no_sqlstate_is_still_retried():
    """The case that matters: `InvalidCachedStatementError` after a migration.

    A cached prepared statement's result types change underneath a live connection, the
    driver raises client-side so there is no SQLSTATE, and the connection is perfectly
    healthy — so the outer transaction would COMMIT and the message would be permanently
    unsearchable, for something one retry fixes. SQLAlchemy's asyncpg adapter maps it to
    NotSupportedError.
    """
    assert _is_transient(_failure(None, cls=NotSupportedError)) is True
    assert _is_transient(_failure(None, cls=InterfaceError)) is True


def test_an_unclassifiable_failure_keeps_the_mail():
    # Fail towards keeping the mail: an unknown failure retried forever is a stuck
    # partition, which is the worse of the two outcomes.
    assert _is_transient(_failure(None)) is False
    assert _is_transient(_failure("")) is False
