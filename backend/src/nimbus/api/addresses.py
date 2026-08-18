"""The valid-address cache — the contract between this API and the Go SMTP receiver.

The receiver answers `RCPT TO` from these Redis sets (docs/ARCHITECTURE.md diagram 8),
which is the last moment mail can be refused. Nothing else reads them.

Redis is a CACHE here, never the source of truth. It is rebuilt from Postgres on every
API startup, so a crash between "transaction committed" and "Redis written" heals itself
the next time the API starts.
"""

import logging

from sqlalchemy import func, select, union
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db
from nimbus.models import Alias, Domain, Mailbox

log = logging.getLogger("nimbus.api")

VALID_ADDRESSES = "valid_addresses"      # SET of "user@domain"
CATCH_ALL_DOMAINS = "catch_all_domains"  # SET of "domain"


async def refresh(session: AsyncSession) -> None:
    """Rebuild the address sets in Redis from Postgres.

    Getting this wrong fails in two different directions:
      • an address MISSING  -> we reject mail that should have been accepted
      • an address STALE    -> we accept mail we then silently drop

    So the swap is atomic: build into a temporary key and RENAME it over the live one.
    A plain DELETE-then-fill would leave a window where the set is empty and the
    receiver rejects every message that arrives during it.
    """
    catch_alls = select(Domain.name).where(
        Domain.catch_all_mailbox_id.is_not(None), Domain.verified
    )
    live = list((await session.scalars(_address_query())).all())

    # A zero-address rebuild is legitimate on an empty database and catastrophic on a
    # full one, and the two look identical from here — `_swap_set` just DELetes the key
    # and every RCPT TO starts answering 550, with no error anywhere.
    #
    # The way that actually happens: this code deployed before its migration ran, so
    # every `domain.verified` is still false. `refresh()` is called from ONE place in
    # production — the lifespan — so it does not retry, and running the migration
    # afterwards fixes nothing until the API is restarted a second time. One count query
    # at startup is what turns that silent outage into a line somebody can find.
    if not live:
        orphaned = await session.scalar(
            select(func.count()).select_from(Mailbox).join(Domain, Domain.id == Mailbox.domain_id)
        )
        if orphaned:
            log.error(
                "address cache is EMPTY but %d mailbox(es) exist — every RCPT TO will be "
                "refused. Almost certainly this build is running against a database that "
                "has not had migration e5b71c04d9a3 applied (block L1 grandfathering). "
                "Run `alembic upgrade head`, then RESTART the API: nothing else rebuilds "
                "this cache.",
                orphaned,
            )

    await _swap_set(VALID_ADDRESSES, live)
    await _swap_set(CATCH_ALL_DOMAINS, list((await session.scalars(catch_alls)).all()))


def _address_query(*extra):
    """Every deliverable address, as `local_part@domain`. Mailboxes and aliases.

    `Domain.verified` lives HERE, in the one query both the startup rebuild and the
    per-domain publish are built from. That is deliberate: block L1 (HLD §9.6a) is
    enforced only by keeping unverified addresses out of Redis, so the filter must not
    be something a second call site can forget. The Go receiver has no idea this rule
    exists — it just does not find the address, and answers 550 through the path it
    already had.
    """
    return union(
        select(Mailbox.local_part.concat("@").concat(Domain.name))
        .join(Domain, Domain.id == Mailbox.domain_id)
        .where(Domain.verified, *extra),
        select(Alias.local_part.concat("@").concat(Domain.name))
        .join(Domain, Domain.id == Alias.domain_id)
        .where(Domain.verified, *extra),
    )


async def add(addresses: list[str]) -> None:
    """Publish just these addresses, without touching the rest of the set.

    Provisioning used to call refresh(), which re-reads every mailbox and alias in the
    database and rewrites the whole set. At the HLD §13 target of 100k+ mailboxes that
    is a full scan plus a multi-megabyte Redis command on every order — and Redis is
    single-threaded, so it stalls every RCPT TO lookup while it runs.

    A full rebuild is the right thing at startup and the wrong thing on a hot path.
    """
    if addresses:
        await db.redis().sadd(VALID_ADDRESSES, *addresses)


async def publish_domain(session: AsyncSession, domain_id) -> int:
    """Publish every address belonging to one domain. Returns how many.

    Called when a domain passes verification (§9.6a) — the moment its mailboxes are
    allowed to receive mail. Scoped to the one domain for the same reason `add()`
    exists: `refresh()` re-reads every mailbox in the database, which is right at
    startup and wrong on a request path.

    Aliases are included and `orders.py` never creates any, which is exactly why this
    lives here rather than being a list comprehension at the call site — the day an
    alias endpoint appears, verification must not be the place that forgot about it.
    """
    rows = list((await session.scalars(_address_query(Domain.id == domain_id))).all())
    await add(rows)

    # The catch-all set is separate and would otherwise stay stale until the next
    # restart, silently dropping mail to a verified domain's catch-all.
    name = await session.scalar(
        select(Domain.name).where(
            Domain.id == domain_id,
            Domain.verified,
            Domain.catch_all_mailbox_id.is_not(None),
        )
    )
    if name:
        await db.redis().sadd(CATCH_ALL_DOMAINS, name)
    return len(rows)


async def _swap_set(key: str, members: list[str]) -> None:
    if not members:
        await db.redis().delete(key)
        return
    tmp = f"{key}:building"
    pipe = db.redis().pipeline()
    pipe.delete(tmp)
    pipe.sadd(tmp, *members)
    pipe.rename(tmp, key)  # atomic swap
    await pipe.execute()
