"""The search query language: a string a human typed, turned into filters. No I/O.

    from:boss has:attachment before:2026-01-01 invoice

Pure, like `ranges.py`, and for the same reason — this is fiddly logic with no
dependencies, so it can be tested exhaustively with no database, no stack and no mail.

**This function never raises.** A search box that answers `400` because someone typed
`before:yesterday` is a worse product than one that searches for the words
"before yesterday". Anything it does not understand is left alone and passed through as
free text, where Postgres will tokenise it harmlessly.

What is deliberately NOT here: quote handling, `OR`, and negation. Postgres's
`websearch_to_tsquery` already implements all three the way a search box user expects
(`"exact phrase"`, `-exclude`, `or`), so this module never touches a quote character —
it lifts out the `word:value` tokens it recognises and hands the rest of the string over
untouched. Re-implementing that would be hand-writing what a library already does
(CLAUDE.md rule 3).
"""

from __future__ import annotations

import datetime
import re
from dataclasses import dataclass

# `\S+` for the value, so an operator never swallows a space. `from:John Smith` filters
# on "John" and searches for "Smith" — the alternative is guessing where the value ends,
# and guessing wrong silently drops half of what the user typed.
#
# The leading (?<!\S) is not the same as \b: it stops `mailto:bob` and `https://x` from
# being read as operators, because what matters is that the operator starts a word, not
# that a word boundary exists somewhere in the middle of one.
_OPERATOR = re.compile(r"(?<!\S)(from|has|before|after):(\S+)", re.IGNORECASE)

MAX_QUERY_CHARS = 500


@dataclass(slots=True)
class Query:
    """What the user asked for, split into the parts that compile differently."""

    free_text: str = ""
    from_contains: str | None = None
    has_attachment: bool = False
    before: datetime.date | None = None
    after: datetime.date | None = None

    @property
    def is_empty(self) -> bool:
        """Nothing to search on. The router answers 400 rather than returning the inbox."""
        return not (
            self.free_text
            or self.from_contains
            or self.has_attachment
            or self.before
            or self.after
        )


def parse(raw: str) -> Query:
    """Split a query string into structured filters plus the free text that is left.

    A recognised operator whose value does not make sense — `before:banana` — is not an
    error and is not dropped either. It stays in the free text exactly as typed, so the
    user still gets results and can see what happened. Dropping it silently would show
    them a full inbox and let them believe the filter applied.

    Repeated operators: the last one wins. Chosen over erroring because `from:a from:b`
    is far more often a correction than a mistake.
    """
    query = Query()
    consumed: list[tuple[int, int]] = []

    # Truncated ONCE, and the NUL strip happens in the same breath. The spans below index
    # into this exact string, so slicing or rewriting it a second time anywhere would let
    # the two drift and the removal would cut in the wrong place.
    #
    # NUL is not cosmetic here. `str.split()` does not treat `\x00` as whitespace, so
    # `?q=%00abc` would survive into `free_text`, get bound into `websearch_to_tsquery`,
    # and asyncpg would raise CharacterNotInRepertoireError — a 500 on input anyone can
    # send, from the one function whose contract is that it never raises. Stripped here
    # rather than at the call sites because this is the single point every operator value
    # AND the free text both pass through. Same reasoning as `mime._str` on the write side.
    raw = raw[:MAX_QUERY_CHARS].replace("\x00", "")

    for match in _OPERATOR.finditer(raw):
        operator = match.group(1).lower()
        value = match.group(2)

        if operator == "from":
            query.from_contains = value
        elif operator == "has":
            if value.lower() != "attachment":
                continue  # `has:` anything else is not ours — leave it as free text
            query.has_attachment = True
        else:
            parsed_date = _date(value)
            if parsed_date is None:
                continue  # unparseable: stays in the free text, see the docstring
            setattr(query, operator, parsed_date)

        consumed.append(match.span())

    # Only the spans that actually became a filter are removed. Walking backwards keeps
    # the earlier spans' offsets valid as the string shrinks.
    text = raw
    for start, end in reversed(consumed):
        text = text[:start] + text[end:]

    query.free_text = " ".join(text.split())
    return query


def _date(value: str) -> datetime.date | None:
    try:
        return datetime.date.fromisoformat(value)
    except ValueError:
        return None


def as_utc(day: datetime.date) -> datetime.datetime:
    """Midnight UTC on that day, for comparing against a `timestamptz` column.

    Binding a bare `date` against `timestamptz` makes Postgres apply the *server's*
    timezone, so the same query would mean different things on two machines. Being
    explicit here makes it wrong in only one way instead of an unpredictable number.
    """
    # ponytail: dates mean UTC, not the reader's local timezone, so `before:2026-01-01`
    # can exclude a message that arrived on 31 December in Auckland. Ceiling: readers
    # outside UTC see off-by-one-day results at the boundary. Upgrade path: take the
    # offset from the client and apply it here — one function to change, which is why the
    # conversion is not inlined at the call site.
    return datetime.datetime.combine(day, datetime.time.min, tzinfo=datetime.timezone.utc)
