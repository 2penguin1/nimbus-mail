"""Shared setup for the integration tests.

One fixture, and it exists entirely because of block L1 (HLD §9.6a).

Every test in this directory provisions a domain under `.example` and then sends real
SMTP to it. `.example` is reserved by RFC 2606 — there is no zone, no nameserver, and
there never will be — so `POST /v1/domains/{id}/verify` can never pass for any of them,
not even by hand. Since L1, an unverified domain's addresses are kept out of Redis, so
the receiver answers `550 No such user here` and every one of these tests fails on its
first send. Six of them did, which is how this fixture came to exist.

So the tests do exactly what `scripts/verify_domain.py` does for an operator on a local
stack: set the flag directly and publish the addresses. That is honest — these tests are
not testing DNS, they are testing dedup, routing, search, GC and snooze, and the domain
check is a precondition they have no way to satisfy for real.

**The gap this leaves, stated rather than hidden:** `_lookup_txt` and the 404/409/503
mapping in `api/routers/domains.py` have NO integration coverage, because covering them
needs a real DNS zone. Their only automated check is `tests/unit/test_domain_challenge.py`,
which never touches the resolver. The first real exercise of that path will be block K,
against a real domain. Recorded in HLD §9.6a as a known gap.
"""

import pytest
from sqlalchemy import select

from nimbus import db
from nimbus.api import addresses
from nimbus.models import Domain


@pytest.fixture
def verify_domain():
    """`await verify_domain(session, "foo.example")` — mark verified, publish addresses.

    Returns how many addresses went live, so a test can assert on it if it cares.
    """

    async def _verify(session, name: str) -> int:
        domain = await session.scalar(select(Domain).where(Domain.name == name))
        assert domain is not None, f"no domain named {name} — did provisioning fail?"
        domain.verified = True
        await session.commit()
        # A test process never enters the API lifespan, so Redis is not connected yet.
        # Same trap block J's cleanup path hit. Idempotent, so calling it per test is fine.
        await db.connect_redis()
        published = await addresses.publish_domain(session, domain.id)
        assert published, f"{name} verified but published 0 addresses"
        return published

    return _verify
