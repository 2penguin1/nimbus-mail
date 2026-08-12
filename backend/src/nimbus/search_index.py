"""Block F: the searchable text of a message, and the rows that hold it.

Shared, not worker-owned, for one reason: `REGCONFIG`. The worker builds the tsvector
with a dictionary and the API builds its tsquery with one, and if the two ever differ
search returns nothing — no error, no log line, just empty results. One constant both
import is the only version of that which cannot drift. Same reason `storage.py` sits
here: the worker writes chunks, the API reads them.


One `message_index` row per delivered copy, keyed `(mailbox_id, message_id)` — the same
key as `mailbox_message`, because a row here means "this mailbox can find this message".

**Why a row per copy rather than one per message.** The text is identical for every
recipient, so this stores it N times, which sits oddly in a project whose whole thesis is
not storing the same thing twice. It is still right: the search index is
`GIN (mailbox_id, tsv)`, and `mailbox_id` has to be a column ON the indexed row for
Postgres to satisfy the tenant filter inside the index scan instead of rechecking it
after a join. One shared row would mean every search scans a posting list spanning every
tenant in the system and filters afterwards — which is the "one giant global index"
HLD §9.4 rules out. The duplicated text is a few KB of derived words next to a 25 MB
attachment; the dedup engine's savings are in the attachment, and this does not touch it.
That settles the open question in HLD §16.

Nothing here is allowed to cost us the message. See `write()`.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import cast, func, literal, literal_column, select
from sqlalchemy.dialects.postgresql import ARRAY, UUID as PG_UUID, TSVECTOR
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import DBAPIError, InterfaceError, NotSupportedError
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus.models import MessageIndex

log = logging.getLogger("nimbus.search_index")

# The dictionary Postgres stems with. English-only in v1, and that is a real limitation:
# a German mail is still indexed, but "Rechnungen" will not match "Rechnung" because no
# German stemmer runs. Postgres picks the config per call, not per row, so making this
# per-tenant later is a column and a lookup, not a re-index of everything.
REGCONFIG = "english"

# Postgres's signature is `setweight(tsvector, "char")` — `"char"` is a distinct 1-byte
# type, NOT `char(1)` and not `varchar`. A bound Python string arrives as varchar, and
# Postgres finds no function matching it: `setweight(tsvector, character varying) does
# not exist`. Rendering the weight as a bare SQL literal leaves it typed `unknown`, which
# Postgres then resolves to `"char"` on its own. Safe to render inline because these are
# two constants defined here, never anything a user supplies.
_WEIGHT_SUBJECT = literal_column("'A'")
_WEIGHT_REST = literal_column("'B'")

# How much text is fed to `to_tsvector`, in BYTES of UTF-8, PER BAND.
#
# Not a guess, and not the obvious guess either. A tsvector has a hard 1,048,575-byte
# ceiling. Plain distinct words expand about 2.1x — but the worst shape is a URL, because
# Postgres emits THREE lexemes for one token (`url`, `host`, `url_path`). Measured on this
# Postgres 16: 130 KB of `<hex>.co/<hex>` produced a 473,762-byte tsvector, **3.64x**.
# `mime.MAX_BODY_BYTES` caps the stored body at 1 MB of CHARACTERS, which is up to 4 MB of
# UTF-8 — so that cap does not protect this call, and both numbers reading "1 MB" is a
# coincidence.
#
# The budget is JOINT: `write()` concatenates the two bands with `||`, and the concat
# enforces the ceiling too, so it must be cleared by the SUM. At 128 KB per band the
# measured worst case is ~909 KB — inside the ceiling, but at 87% of it, NOT the
# "comfortably half" an earlier version of this comment claimed off the 2.1x figure.
# Real overflow starts around 176 KB per band. Raise this constant and
# `test_the_two_bands_together_stay_under_the_tsvector_ceiling` fails before production
# does — that test exists because this arithmetic has now been wrong twice.
#
# It has to be impossible rather than merely unlikely, because this runs inside the
# delivery transaction: `to_tsvector` raising would roll back the stored message AND its
# delivery, the Kafka offset would never commit, and the worker would redeliver the same
# message forever — one crafted email would stop that partition permanently. That is the
# bug the NUL byte in `mime._str` already caused once. This cap is the first line of
# defence, the savepoint in `write()` the second.
MAX_INDEXED_BYTES = 128 * 1024

# ponytail: naive tag strip, used ONLY when a mail has no plain-text part. It does not
# decode entities (`&amp;` stays literal) and does not drop <script>/<style> contents.
# Ceiling: an HTML-only mail may index a few junk words. Upgrade path: stdlib
# html.parser, when HTML-only mail proves common enough in the corpus to matter.
_TAGS = re.compile(r"<[^>]*>")


@dataclass(slots=True)
class SearchText:
    """The two weight bands. Kept apart so `subject` can outrank the body in ranking."""

    subject: str  # weight A
    body: str     # weight B


def build(
    subject: str | None,
    from_addr: str | None,
    filenames: list[str | None],
    body_text: str | None,
    body_html: str | None,
) -> SearchText:
    """Everything about a message that should be findable, in two weight bands. Pure.

    Attachment filenames go in because "that PDF called Q3" is how people actually look
    for mail. Attachment CONTENTS do not — extracting text from PDFs and Office files is
    a different project, and HLD §4 does not ask for it.

    `body_html` is used only as a fallback when there is no plain-text part. Indexing raw
    markup would make every message match `div`, `href` and `style`.
    """
    body = body_text
    if not body:
        body = _TAGS.sub(" ", body_html) if body_html else ""

    parts = [from_addr or "", *(name or "" for name in filenames), body]
    return SearchText(
        subject=_clean(subject or ""),
        body=_clean(" ".join(parts)),
    )


def _clean(text: str) -> str:
    """Strip NUL, then truncate to MAX_INDEXED_BYTES without splitting a character.

    Bytes, not characters: the tsvector ceiling is counted in bytes, and one emoji is
    four of them. Truncating by `len(str)` would let a 256,000-character CJK body through
    as 768 KB. Both bands share the one cap, which is what the JOINT budget above assumes.

    The NUL strip repeats `mime._str` on purpose — cheap insurance against the stuck
    partition described above, reachable from a second direction.
    """
    text = text.replace("\x00", "")
    raw = text.encode("utf-8")
    if len(raw) <= MAX_INDEXED_BYTES:
        return text
    # errors="ignore" drops a partial character at the cut rather than raising.
    return raw[:MAX_INDEXED_BYTES].decode("utf-8", errors="ignore")


async def write(
    session: AsyncSession,
    message_id: uuid.UUID,
    mailbox_ids: set[uuid.UUID],
    text: SearchText,
) -> int:
    """Insert one index row per mailbox. Returns how many were written.

    `mailbox_ids` must be the set `routing.deliver()` actually created, never the set it
    was asked to deliver to. That is what makes this idempotent on a Kafka replay: a
    replay creates no `mailbox_message` rows, so it indexes nothing. The composite
    foreign key added in migration `4f1a9c2b8e07` enforces it — an index row for a
    delivery that does not exist is a constraint violation, not a phantom search hit.

    **The tsvector is computed once, not once per mailbox.** The subquery below is
    uncorrelated, so Postgres hoists it to an InitPlan and evaluates it exactly once.
    Measured on this machine: `to_tsvector` over a 200 KB body costs 22 ms, and inserting
    100 rows takes 105 ms hoisted against 1810 ms evaluated per row. Per-row would add
    ~2 seconds to a 100-recipient delivery, holding row locks while the Kafka session
    timeout runs — block C already came close to a consumer-group eviction at 627 ms.
    Verified with EXPLAIN ANALYZE at this exact shape: `InitPlan 1 ... loops=1`.

    **A failure here must not cost the message.** The savepoint is the point: this is the
    last step of a transaction that has already uploaded bytes to S3 and delivered mail,
    and rolling all of that back over a search-index problem would send the message round
    the redelivery loop again. Unsearchable-but-delivered is recoverable; undelivered is
    not.
    """
    if not mailbox_ids:
        return 0

    tsv = select(
        func.setweight(
            func.to_tsvector(REGCONFIG, text.subject), _WEIGHT_SUBJECT, type_=TSVECTOR
        ).op("||")(
            func.setweight(
                func.to_tsvector(REGCONFIG, text.body), _WEIGHT_REST, type_=TSVECTOR
            )
        )
    ).scalar_subquery()

    statement = (
        pg_insert(MessageIndex)
        .from_select(
            ["mailbox_id", "message_id", "tsv"],
            # `SELECT unnest($1::uuid[])` — one bind parameter for the whole fan-out, so
            # a 100-recipient message is one statement rather than 100.
            select(
                func.unnest(
                    cast(sorted(mailbox_ids), ARRAY(PG_UUID(as_uuid=True)))
                ).label("mailbox_id"),
                literal(message_id),
                tsv,
            ),
        )
        .on_conflict_do_nothing(
            index_elements=[MessageIndex.mailbox_id, MessageIndex.message_id]
        )
    )

    try:
        # A SAVEPOINT, so a failure rolls back only this statement. Without it, Postgres
        # marks the whole transaction aborted and every later statement fails too — the
        # message would be lost even though the problem was only its index row.
        async with session.begin_nested():
            result = await session.execute(statement)
        return result.rowcount
    except DBAPIError as failure:
        # Which failures are ours to swallow, decided by SQLSTATE rather than by
        # exception class. SQLAlchemy surfaces a tsvector overflow as a bare DBAPIError —
        # verified: SQLSTATE 54000, no narrower subclass — so `except DataError` would
        # not catch it and `except DBAPIError` catches far too much.
        if _is_transient(failure):
            # A deadlock, a serialization failure, a statement timeout or a dropped
            # connection says nothing about this message. Swallowing it would commit the
            # mail unindexed for a reason that would have gone away on the next attempt.
            # Let it out: worker/main.py retries the whole event.
            raise
        # Everything else means this particular message cannot be indexed, and no retry
        # will change that. Keep the mail, lose the index row, make the noise loud enough
        # to find. ERROR, not WARNING — mail nobody can search for is a defect.
        #
        # ponytail: there is no repair path. This message stays unsearchable forever, and
        # so does anything delivered before block F shipped. Ceiling: the log line is the
        # only record. Upgrade path: `scripts/reindex.py` walking `mailbox_message` and
        # calling `write()` again — every input it needs is a stored column now, so it
        # needs neither S3 nor a MIME parse. Cheap to write when there is data worth
        # repairing; there is none today.
        log.error(
            "search index write FAILED for message=%s (%d mailbox(es)); "
            "the message is still stored and delivered, but will not be findable",
            message_id, len(mailbox_ids), exc_info=True,
        )
        return 0


# SQLSTATE classes that mean "try again", not "this message is bad":
#   08 connection exception      40 transaction rollback (deadlock, serialization)
#   53 insufficient resources (disk full, out of memory)
#   57 operator intervention (admin shutdown, cancelled statement)
#
# `57` is safe only because nothing sets `statement_timeout` — so 57014 can currently
# only come from an operator cancelling or the server shutting down, both genuinely
# transient. Add a statement_timeout and 57014 becomes DETERMINISTIC for an expensive
# query, at which point retrying it forever is a stuck partition. Move `57` out of this
# set in the same change that adds the timeout.
_TRANSIENT = {"08", "40", "53", "57"}


def _is_transient(failure: DBAPIError) -> bool:
    """Should the whole event be retried, or is this message simply un-indexable?

    Classified by SQLSTATE, not by exception class: SQLAlchemy surfaces a tsvector
    overflow as a bare `DBAPIError` (verified — SQLSTATE 54000, no narrower subclass), so
    `except DataError` would miss it and `except DBAPIError` catches far too much.

    Some failures carry no SQLSTATE at all, because they never reached Postgres.
    SQLAlchemy's asyncpg adapter sets `sqlstate` from the driver error, and a client-side
    one has none — the case that matters is `InvalidCachedStatementError`, raised when a
    cached prepared statement's result types changed underneath a live connection, which
    is exactly what an Alembic migration does. Its connection is healthy, so the outer
    transaction would COMMIT and the message would be permanently unsearchable — for a
    failure that one retry fixes. Those arrive as `NotSupportedError`/`InterfaceError`,
    so they are treated as transient despite having nothing to classify on.
    """
    if isinstance(failure, (InterfaceError, NotSupportedError)):
        return True
    sqlstate = getattr(failure.orig, "sqlstate", None) or ""
    return sqlstate[:2] in _TRANSIENT
