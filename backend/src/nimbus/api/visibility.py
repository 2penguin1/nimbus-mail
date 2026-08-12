"""Which mailboxes a logged-in user may read.

Every read endpoint in block E filters on this and nothing else. It exists as one
function, in one place, because the alternative is remembering to write the same filter
on six endpoints — and the one you forget is a cross-tenant data leak, which HLD §10.1
calls the worst bug this system can have.

**Why it is not simply "your own mailbox".** Block D delivers ONE copy to a shared
mailbox and grants its members read access instead of giving each of them a copy
(HLD §9.2). That was a storage decision — K members cost 1 row instead of K — and this
is where the bill for it lands: the fan-out that delivery skipped has to happen here,
on read.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus.models import Domain, Mailbox, SharedMailboxMember


async def readable_mailboxes(
    session: AsyncSession, mailbox_id: uuid.UUID
) -> set[uuid.UUID]:
    """Your own mailbox, plus every shared mailbox you are a member of — in your tenant.

    The reseller check is not belt-and-braces. `shared_mailbox_member` is two plain
    foreign keys to `mailbox.id` (models.py), so the schema happily permits a row that
    joins mailboxes belonging to two different customers. One such row — from the
    member-management endpoint nobody has written yet, or an operator script — and a
    user in one tenant reads another tenant's inbox. Every counter stays consistent, no
    log fires, and the only symptom is a customer seeing another company's mail.

    HLD §9.2 made exactly this check mandatory on the delivery side for
    `alias.target_mailbox_id` and `domain.catch_all_mailbox_id`. This is its read-side
    twin, and it belongs here rather than at six call sites for the same reason.
    """
    mine = (
        select(Domain.reseller_id)
        .join(Mailbox, Mailbox.domain_id == Domain.id)
        .where(Mailbox.id == mailbox_id)
        .scalar_subquery()
    )
    shared = (
        await session.scalars(
            select(SharedMailboxMember.shared_mailbox_id)
            .join(Mailbox, Mailbox.id == SharedMailboxMember.shared_mailbox_id)
            .join(Domain, Domain.id == Mailbox.domain_id)
            .where(
                SharedMailboxMember.member_mailbox_id == mailbox_id,
                Domain.reseller_id == mine,
            )
        )
    ).all()
    return {mailbox_id, *shared}
