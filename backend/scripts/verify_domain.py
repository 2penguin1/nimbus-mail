"""Mark a domain verified without the DNS check. Operator tool — block L1, HLD §9.6a.

    cd backend
    uv run python scripts/verify_domain.py acme.example
    uv run python scripts/verify_domain.py --list

**Why this exists, in one line:** a domain that has no real DNS zone can never pass
`POST /v1/domains/{id}/verify`, and without an escape hatch the local stack stops
working the moment L1 ships.

That is not hypothetical. Every integration test and `scripts/loadtest.py` provisions a
domain like `lt-abc.example` — a reserved name (RFC 2606) that by definition has no
nameserver. After L1 those domains are unverified, so `api/addresses.py` keeps their
addresses out of Redis, and every SMTP send gets `550`. The whole local stack goes dark.

**Why it is a script and not an endpoint.** It is the one operation that bypasses the
only proof of ownership Nimbus has. Over HTTP it would be a hole with an auth check in
front of it; as a script it needs a shell on the box and the database password, which is
the same bar `create_reseller.py` already sets. There is deliberately no config flag and
no environment switch to disable verification globally — a security control with an off
switch is one wrong env var away from being off in production, which is the exact
failure L1 exists to prevent. This turns it off for ONE named domain, out loud.

**In production, do not use this.** Use the DNS check. If you find yourself reaching for
this against a real domain, the reseller has not proved anything and the record is wrong.
"""

import asyncio
import sys

from sqlalchemy import select

from nimbus import db
from nimbus.api import addresses
from nimbus.models import Domain


async def verify(name: str) -> int:
    """Mark one domain verified and publish its addresses. Returns addresses published.

    Idempotent: verifying an already-verified domain republishes its addresses, which is
    a no-op in Redis and repairs the set if it was ever lost.
    """
    async with db.Session() as session:
        domain = await session.scalar(select(Domain).where(Domain.name == name))
        if domain is None:
            raise SystemExit(f"no such domain: {name}")
        domain.verified = True
        await session.commit()
        # Redis is only reachable after connect_redis(); a script never enters the API
        # lifespan that normally does it. Same trap loadtest.py hit in block J.
        await db.connect_redis()
        return await addresses.publish_domain(session, domain.id)


async def show() -> None:
    async with db.Session() as session:
        rows = (await session.scalars(select(Domain).order_by(Domain.name))).all()
    for d in rows:
        print(f"{'VERIFIED' if d.verified else '  --    '}  {d.name}")
    if not rows:
        print("no domains")


async def main() -> None:
    if len(sys.argv) < 2:
        sys.exit(__doc__.strip().splitlines()[0] + "\n\nusage: verify_domain.py <domain>|--list")

    if sys.argv[1] == "--list":
        await show()
    else:
        name = sys.argv[1].strip().lower()
        published = await verify(name)
        print(f"{name} verified; {published} address(es) published to Redis")

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
