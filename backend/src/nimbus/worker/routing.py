"""Block D: decide which mailboxes get the message, then write the fan-out rows.

Two steps, deliberately separate:

    resolve()   address  -> mailbox ids     (pure lookup, writes nothing)
    deliver()   mailbox ids -> rows         (the only place blob.refcount goes UP)

**The chain, first match wins** (docs/HLD.md §9.2):

    1. an alias?              -> its target mailbox
    2. a mailbox?             -> itself
    3. domain has a catch-all -> the catch-all mailbox
    4. otherwise              -> drop and log

Step 4 is not a bounce and cannot be. The address was accepted at `RCPT TO` and the SMTP
connection closed minutes ago — there is nobody left to say `550` to, and outbound mail
is an explicit non-goal (§4). In practice it only fires on a race: the mailbox was
deleted between `RCPT TO` and now.

**Shared mailboxes are NOT expanded here.** A shared mailbox receives ONE copy;
`shared_mailbox_member` says who may READ it, which is what that table has always
documented itself as. So `is_shared` changes nothing about delivery — it is a read-path
concern for block E. One row instead of K, one refcount instead of K, and read state is
shared, which is what a team support inbox actually wants: when Alice opens it, Bob can
see it has been handled.

**Forwarding rules are not consulted at all.** Forwarding needs an outbound SMTP client,
which §4 rules out of v1, and acting on `keep_copy = false` without one would delete the
only copy and send nothing — losing mail for a feature that does not exist. Nothing
writes `forwarding_rule` either, so the copy is simply always kept. The marker at the
bottom of this file says what has to change together when that stops being true.
"""

# ponytail: shared mailboxes get ONE row, not one per member. Ceiling: no per-member
# read state — if Alice opens it, Bob sees it opened. Upgrade path: block E fans out on
# READ (union the mailboxes you own with the ones you are a member of), which keeps the
# single stored copy. Do NOT "fix" it by fanning out on write.

import datetime
import logging
import uuid

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus.models import (
    Alias,
    Blob,
    Domain,
    Mailbox,
    MailboxMessage,
    MessageAttachment,
)

log = logging.getLogger("nimbus.worker.routing")


async def resolve(
    session: AsyncSession, reseller_id: uuid.UUID, addresses: list[str]
) -> set[uuid.UUID]:
    """Which mailboxes should receive this message. Reads only, writes nothing.

    Returns a SET, so two addresses landing on the same mailbox — say `sales@` aliased
    to Alice, plus `alice@` directly — deliver once. Delivering twice would put the
    message in her inbox twice and count `blob.refcount` twice, and that second count
    never comes back down, so GC could never free the blob.

    Every lookup is batched. One email may carry 100 recipients (`MaxRecipients`), and
    a query per recipient would be 300 round trips on the delivery path.
    """
    parsed = []
    for address in addresses:
        local_part, _, domain_name = address.rpartition("@")
        if local_part and domain_name:
            parsed.append((address, local_part, domain_name))
    if not parsed:
        return set()

    domain_names = {d for _, _, d in parsed}
    local_parts = {lp for _, lp, _ in parsed}

    # Scoped to this reseller. A domain name is globally unique, but filtering here is
    # what stops a bug elsewhere from delivering one tenant's mail into another's.
    domains = {
        name: (domain_id, catch_all)
        for domain_id, name, catch_all in (
            await session.execute(
                select(Domain.id, Domain.name, Domain.catch_all_mailbox_id).where(
                    Domain.reseller_id == reseller_id, Domain.name.in_(domain_names)
                )
            )
        ).all()
    }
    if not domains:
        return set()
    domain_ids = [domain_id for domain_id, _ in domains.values()]

    aliases = {
        (domain_id, local_part): target
        for domain_id, local_part, target in (
            await session.execute(
                select(Alias.domain_id, Alias.local_part, Alias.target_mailbox_id).where(
                    Alias.domain_id.in_(domain_ids), Alias.local_part.in_(local_parts)
                )
            )
        ).all()
    }
    mailboxes = {
        (domain_id, local_part): mailbox_id
        for mailbox_id, domain_id, local_part in (
            await session.execute(
                select(Mailbox.id, Mailbox.domain_id, Mailbox.local_part).where(
                    Mailbox.domain_id.in_(domain_ids), Mailbox.local_part.in_(local_parts)
                )
            )
        ).all()
    }

    targets: set[uuid.UUID] = set()
    for address, local_part, domain_name in parsed:
        entry = domains.get(domain_name)
        if entry is None:
            log.warning("domain not ours any more, dropping: %s", address)
            continue
        domain_id, catch_all = entry

        # The chain. Alias before mailbox, because an alias is an explicit redirect and
        # a same-named mailbox would otherwise silently win.
        target = aliases.get((domain_id, local_part))
        if target is None:
            target = mailboxes.get((domain_id, local_part))
        if target is None:
            target = catch_all
        if target is None:
            # Accepted at RCPT TO, unroutable now. Log and drop — see the module note.
            log.warning("no route, dropping: %s", address)
            continue
        targets.add(target)

    return await _only_ours(session, reseller_id, targets)


async def _only_ours(
    session: AsyncSession, reseller_id: uuid.UUID, targets: set[uuid.UUID]
) -> set[uuid.UUID]:
    """Drop any mailbox that does not belong to this reseller. The last gate.

    The lookups above scope their KEYS correctly — domains by `reseller_id`, aliases and
    mailboxes by that reseller's `domain_id`s. What they cannot scope is where a pointer
    POINTS. `alias.target_mailbox_id` and `domain.catch_all_mailbox_id` are plain foreign
    keys to `mailbox.id`, and the schema happily lets either name a mailbox in a
    different tenant.

    Nothing can create that pointer today — the provisioning API writes domains and
    mailboxes only. It becomes reachable the moment block E adds alias or catch-all
    endpoints, and the failure is silent: reseller B's user reads reseller A's mail while
    every counter stays perfectly consistent. Nothing would ever flag it.

    One query, checked here rather than at each call site, because `resolve()` is the
    only thing that produces mailbox ids and this is where they all converge.
    """
    if not targets:
        return targets
    ours = set(
        (
            await session.scalars(
                select(Mailbox.id)
                .join(Domain, Domain.id == Mailbox.domain_id)
                .where(Mailbox.id.in_(targets), Domain.reseller_id == reseller_id)
            )
        ).all()
    )
    for stranger in targets - ours:
        log.error(
            "REFUSING cross-tenant delivery: mailbox %s is not reseller %s's. "
            "An alias or catch-all points outside its own tenant.",
            stranger, reseller_id,
        )
    return ours


async def deliver(
    session: AsyncSession,
    reseller_id: uuid.UUID,
    message_id: uuid.UUID,
    mailbox_ids: set[uuid.UUID],
    size_bytes: int,
    received_at: datetime.datetime,
) -> set[uuid.UUID]:
    """Write the fan-out rows and move the counters. Returns the mailboxes actually written.

    `blob.refcount` counts `mailbox_message` rows, so it moves by the number of rows
    this call really inserted — never by the number of recipients. Two addresses can
    resolve to one mailbox, and a replay can find rows already there. Counting
    recipients instead would leave refcounts permanently high and GC would free nothing.

    Returns the SET, not a count, because block F's search index needs to know *which*
    mailboxes gained a copy so it can write one index row for each. That set is also the
    replay guard both blocks share: a redelivery inserts no rows, so it returns empty,
    and neither the refcounts nor the index move.
    """
    if not mailbox_ids:
        return set()

    # ON CONFLICT DO NOTHING on the (mailbox_id, message_id) primary key makes this
    # idempotent, and RETURNING tells us which rows were genuinely new. One statement
    # rather than one per mailbox: a 100-recipient message is 1 round trip, not 100.
    created = list(
        (
            await session.scalars(
                pg_insert(MailboxMessage)
                .values([
                    # When the mail ARRIVED, not when we got round to it. This column,
                    # not message.received_at, is what mailbox_message_list_idx sorts an
                    # inbox on. Left to its now() default, draining a Kafka backlog would
                    # stamp every queued message with the drain time and an inbox would
                    # show them in partition order instead of arrival order.
                    {
                        "mailbox_id": mailbox_id,
                        "message_id": message_id,
                        "received_at": received_at,
                    }
                    for mailbox_id in sorted(mailbox_ids)
                ])
                .on_conflict_do_nothing(
                    index_elements=[MailboxMessage.mailbox_id, MailboxMessage.message_id]
                )
                .returning(MailboxMessage.mailbox_id)
            )
        ).all()
    )
    if not created:
        return set()

    # Logical usage, HLD §11. The delete trigger subtracts `message.size_bytes`, so this
    # must add the same number or the two drift apart and a mailbox's usage slowly rots.
    await session.execute(
        update(Mailbox)
        .where(Mailbox.id.in_(created))
        .values(used_bytes=Mailbox.used_bytes + size_bytes)
    )

    # Every blob this message carries gains one reference per row written. This is the
    # ONLY place blob.refcount goes up; the mailbox_message delete trigger is the only
    # place it comes down.
    await session.execute(
        update(Blob)
        .where(
            Blob.reseller_id == reseller_id,
            Blob.hash.in_(
                select(MessageAttachment.blob_hash).where(
                    MessageAttachment.message_id == message_id,
                    # reseller_id is a caller-supplied argument. Filtering the subquery
                    # on it too makes a mismatched call impossible instead of unlikely.
                    MessageAttachment.reseller_id == reseller_id,
                )
            ),
        )
        # refcount_zeroed_at back to NULL: this blob is in use again. The column's
        # invariant is "NULL exactly when refcount > 0", and refcount only ever goes UP
        # here, so NULL is unconditionally right. Leaving it set would let GC collect a
        # blob that a delivery had just resurrected, one grace period later — the clock
        # would still be reading the moment it was last garbage.
        .values(refcount=Blob.refcount + len(created), refcount_zeroed_at=None)
    )
    return set(created)


# ponytail: `forwarding_rule` is not consulted at all. Nothing writes that table — the
# provisioning API has no endpoint for it — so a query here would return nothing on every
# single message, forever. Ceiling: `keep_copy = false` is ignored, and a rule that does
# somehow exist is silently inert. Upgrade path: block E adds the endpoint AND an
# outbound SMTP client (§4 non-goal today); bring the lookup back in the same change,
# never one without the other.
