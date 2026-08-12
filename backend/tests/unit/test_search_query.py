"""The query parser. No database, no stack — it is a pure function, so this is all of it.

The property that matters most is the last group: **it must never raise.** This function
sits directly behind a search box on the public API, so anything it throws becomes a 500
on input a user typed by accident.
"""

import datetime

from nimbus.api.search_query import MAX_QUERY_CHARS, Query, as_utc, parse

UTC = datetime.timezone.utc


# --------------------------------------------------------------------------
# The operators
# --------------------------------------------------------------------------

def test_bare_words_are_free_text():
    assert parse("quarterly invoice") == Query(free_text="quarterly invoice")


def test_from_is_lifted_out_of_the_text():
    q = parse("from:boss invoice")
    assert q.from_contains == "boss"
    assert q.free_text == "invoice"


def test_has_attachment():
    q = parse("has:attachment")
    assert q.has_attachment is True
    assert q.free_text == ""


def test_dates():
    q = parse("after:2026-01-01 before:2026-02-01 report")
    assert q.after == datetime.date(2026, 1, 1)
    assert q.before == datetime.date(2026, 2, 1)
    assert q.free_text == "report"


def test_the_hld_example_parses_whole():
    # docs/HLD.md §9.4 quotes exactly this string. If it stops working, the doc is a lie.
    q = parse("from:boss has:attachment before:2026-01-01 invoice")
    assert q == Query(
        free_text="invoice",
        from_contains="boss",
        has_attachment=True,
        before=datetime.date(2026, 1, 1),
    )


def test_operators_are_case_insensitive():
    assert parse("FROM:boss").from_contains == "boss"
    assert parse("Has:Attachment").has_attachment is True


def test_last_one_wins():
    assert parse("from:alice from:bob").from_contains == "bob"


# --------------------------------------------------------------------------
# Everything it does not understand degrades to free text — it never errors
# --------------------------------------------------------------------------

def test_unknown_operator_stays_in_the_text():
    # `to:` is not one of ours. It must not vanish, and it must not raise.
    q = parse("to:jane hello")
    assert q.free_text == "to:jane hello"
    assert q.from_contains is None


def test_malformed_date_stays_in_the_text():
    q = parse("before:banana invoice")
    assert q.before is None
    assert q.free_text == "before:banana invoice", (
        "a bad date must stay visible; dropping it shows a full inbox and lets the "
        "user believe the filter applied"
    )


def test_has_something_else_is_not_a_filter():
    q = parse("has:wings")
    assert q.has_attachment is False
    assert q.free_text == "has:wings"


def test_a_url_is_not_an_operator():
    # `https://x` and `mailto:bob` both contain `word:value`. A plain \b would match
    # inside them and silently eat half the query.
    q = parse("mailto:bob@acme.com")
    assert q.free_text == "mailto:bob@acme.com"
    assert q.from_contains is None


def test_websearch_syntax_is_passed_through_untouched():
    # Quotes, negation and `or` belong to Postgres's websearch_to_tsquery. This parser
    # must not touch them, or phrase search silently stops working.
    q = parse('from:boss "exact phrase" -excluded or other')
    assert q.free_text == '"exact phrase" -excluded or other'
    assert q.from_contains == "boss"


def test_nothing_raises_on_hostile_input():
    for hostile in [
        "", "   ", ":", "::::", "from:", "from::", "before:", "-", "&", "|",
        "from:'; DROP TABLE message; --", "\x00", "\n\t", "a" * 10_000,
        "from:" + "x" * 5_000, "before:9999-99-99", "before:2026-1", "has:",
    ]:
        parse(hostile)  # the assertion is that this line does not raise


def test_nul_bytes_never_reach_the_database():
    """`?q=%00abc` must not become a 500.

    Postgres TEXT cannot hold 0x00, so a NUL surviving into `free_text` or into an
    operator value is bound into `websearch_to_tsquery` or `ILIKE` and asyncpg raises
    CharacterNotInRepertoireError. `str.split()` does NOT treat `\\x00` as whitespace, so
    the whitespace collapse at the end of parse() does not remove it — this needs its own
    strip, and it is the same failure `mime._str` prevents on the write side.
    """
    assert "\x00" not in parse("\x00abc").free_text
    assert "\x00" not in parse("hello\x00world").free_text
    assert "\x00" not in (parse("from:a\x00b").from_contains or "")
    assert "\x00" not in parse("\x00").free_text
    # Still a usable query, not an emptied one — strip the byte, keep the words.
    assert parse("\x00invoice").free_text == "invoice"


def test_empty_query_is_detectable():
    assert parse("").is_empty is True
    assert parse("   ").is_empty is True
    assert parse("has:wings").is_empty is False  # degraded to free text, still a search
    assert parse("has:attachment").is_empty is False


def test_long_input_is_truncated_not_rejected():
    q = parse("from:boss " + "x" * (MAX_QUERY_CHARS * 4))
    assert q.from_contains == "boss"
    assert len(q.free_text) <= MAX_QUERY_CHARS


# --------------------------------------------------------------------------
# Date conversion
# --------------------------------------------------------------------------

def test_as_utc_is_midnight_and_carries_a_timezone():
    moment = as_utc(datetime.date(2026, 1, 1))
    assert moment == datetime.datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    # Without tzinfo, Postgres compares against a timestamptz using the SERVER's
    # timezone, so the same query would mean different things on two machines.
    assert moment.tzinfo is not None
