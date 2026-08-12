"""The valid-address cache — the contract between this API and the Go SMTP receiver.

The receiver answers `RCPT TO` from these Redis sets (docs/ARCHITECTURE.md diagram 8),
which is the last moment mail can be refused. Nothing else reads them.

Redis is a CACHE here, never the source of truth. It is rebuilt from Postgres on every
API startup, so a crash between "transaction committed" and "Redis written" heals itself
the next time the API starts.
"""

from sqlalchemy import select, union
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db
from nimbus.models import Alias, Domain, Mailbox

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
    addresses = union(
        select(Mailbox.local_part.concat("@").concat(Domain.name))
        .join(Domain, Domain.id == Mailbox.domain_id),
        select(Alias.local_part.concat("@").concat(Domain.name))
        .join(Domain, Domain.id == Alias.domain_id),
    )
    catch_alls = select(Domain.name).where(Domain.catch_all_mailbox_id.is_not(None))

    await _swap_set(VALID_ADDRESSES, list((await session.scalars(addresses)).all()))
    await _swap_set(CATCH_ALL_DOMAINS, list((await session.scalars(catch_alls)).all()))


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
