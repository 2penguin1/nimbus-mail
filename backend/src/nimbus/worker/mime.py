"""Opening the envelope — the only file that touches the stdlib `email` API.

An email is one long text stream. MIME is the convention that lets it carry a subject,
an HTML body and three PDFs at once, marked off by boundary lines. Reading that back out
is what this module does.

We do not hand-write the parser (CLAUDE.md rule 3, HLD §15). Real-world mail has forty
years of malformed edge cases in it, and the stdlib already handles them.

`policy=email.policy.default` is not optional. Without it the parser returns the legacy
`Message` class, which has no `iter_attachments()` and different header handling — the
code below would fail in ways that look like bad input rather than a wrong parser.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import parsedate_to_datetime
from typing import IO

log = logging.getLogger("nimbus.worker.mime")


@dataclass(slots=True)
class Attachment:
    filename: str | None
    content_type: str
    data: bytes


@dataclass(slots=True)
class Headers:
    message_id: str | None
    in_reply_to: str | None
    subject: str | None
    from_addr: str | None
    sent_at: datetime | None


def parse(fp: IO[bytes]) -> EmailMessage:
    """Parse a file-like stream of raw message bytes.

    Takes a stream, not bytes, so an S3 response body can be handed straight in — the
    parser reads it incrementally instead of us buffering the whole message first.
    """
    return BytesParser(policy=policy.default).parse(fp)


def headers(msg: EmailMessage) -> Headers:
    """Pull the few headers we store as columns.

    Every one of these is attacker-supplied — a sender writes whatever it likes. They are
    stored and displayed, never trusted for authorisation, and `sent_at` in particular is
    only what the sender CLAIMS. `message.received_at` is when we actually got it, and
    that is the one to sort by.
    """
    sent_at = None
    raw_date = msg["date"]
    if raw_date:
        try:
            sent_at = parsedate_to_datetime(raw_date)
        except (TypeError, ValueError):
            # A malformed Date: header must not cost us the whole message.
            log.warning("unparseable Date header: %r", raw_date)

    return Headers(
        message_id=_str(msg["message-id"]),
        in_reply_to=_str(msg["in-reply-to"]),
        subject=_str(msg["subject"]),
        from_addr=_str(msg["from"]),
        sent_at=sent_at,
    )


# A text body larger than this is pathological, and Postgres refuses a `tsvector` over
# 1 MB outright — so block F could not index it anyway. Truncating beats storing
# something search can never see.
MAX_BODY_BYTES = 1024 * 1024


def body(msg: EmailMessage) -> tuple[str | None, str | None]:
    """The readable part of the email: (plain text, HTML). Either may be absent.

    `get_body(preferencelist=...)` is the stdlib walking the MIME tree and picking the
    best candidate part — it knows that `multipart/alternative` means "same content,
    pick one" while `multipart/mixed` means "all of it". Hand-walking that is exactly
    what CLAUDE.md rule 3 forbids.

    Both strings are stripped of NUL bytes for the same reason headers are: a sender can
    put `0x00` in a body, Postgres `TEXT` cannot hold it, and the failed insert would
    roll back the transaction and have the message redelivered forever.
    """
    return _part_text(msg, "plain"), _part_text(msg, "html")


def _part_text(msg: EmailMessage, subtype: str) -> str | None:
    part = msg.get_body(preferencelist=(subtype,))
    if part is None:
        return None
    try:
        content = part.get_content()
    except (LookupError, UnicodeDecodeError) as exc:
        # An unknown charset must cost us the body, never the whole message.
        log.warning("cannot decode %s body: %s", subtype, exc)
        return None
    if len(content) > MAX_BODY_BYTES:
        log.warning("truncating %s body of %d bytes", subtype, len(content))
        content = content[:MAX_BODY_BYTES]
    return content.replace("\x00", "")


def attachments(msg: EmailMessage) -> list[Attachment]:
    """Every attached file, decoded back to its original bytes.

    `get_payload(decode=True)` is what undoes base64. Attachments travel base64-encoded
    because SMTP only carries text, which inflates binary by about a third — so this is
    also the step where a 35 MB message becomes the 25 MB file the sender attached. We
    must hash the DECODED bytes: the same file re-encoded by a different mail client can
    differ byte-for-byte on the wire while being identical once decoded, and hashing the
    encoded form would silently miss those duplicates.

    `iter_attachments()` skips the message body itself, which is what we want — HLD §16
    settles that only attachments are deduplicated, never the body.
    """
    parts = list(msg.iter_attachments())

    # iter_attachments() returns nothing at all for a message that is not multipart —
    # but a single-part message whose own Content-Disposition says `attachment` IS one
    # file and nothing else. Rare, legal, and without this the file vanishes with no
    # error anywhere: the message stores with zero attachments and looks fine.
    if not parts and msg.get_content_disposition() == "attachment":
        parts = [msg]

    found = []
    for part in parts:
        data = part.get_payload(decode=True)
        if data is None:
            # A forwarded email arrives as message/rfc822, and its payload is a parsed
            # message object rather than bytes, so decode=True gives None. Serialising
            # it back to the .eml it came from is what we store. Skipping it instead
            # would silently drop every forwarded-as-attachment mail.
            if part.get_content_maintype() == "message":
                nested = part.get_payload(0)
                data = nested.as_bytes() if nested is not None else None
        if data is None:
            # A nested multipart genuinely has no payload of its own. Nothing to store.
            continue
        found.append(
            Attachment(
                # Through _str for the same reason the headers are: a filename is
                # sender-supplied and may carry a NUL byte.
                filename=_str(part.get_filename()),
                content_type=_str(part.get_content_type()),
                data=data,
            )
        )
    return found


def _str(value) -> str | None:
    """Header objects are not plain strings, and asyncpg will not bind them.

    The NUL strip is not cosmetic. RFC 5322 forbids NUL in a header body, but a sender
    can put one there anyway and the stdlib parser passes it through unchanged. Postgres
    TEXT cannot hold 0x00 at all — asyncpg raises CharacterNotInRepertoireError, the
    whole transaction rolls back, the Kafka offset is never committed, and the worker
    redelivers the same message forever. One `Subject: a\\x00b` from anyone on the
    internet would stop that partition permanently. Stripped here, in the one place
    every attacker-supplied string passes through, rather than at five call sites.
    """
    return str(value).replace("\x00", "") if value is not None else None
