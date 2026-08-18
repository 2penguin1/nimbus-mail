"""Domain ownership — docs/HLD.md §9.6a. Reseller API key, not a mailbox token.

A reseller can name any domain it likes in `POST /v1/orders`. This module is what stops
that from MEANING anything until they have proved they control the domain's DNS.

The proof is the standard one — publish a value we give you at a name only the zone's
owner can write — and it is the same bar Google Workspace, AWS SES and Let's Encrypt set.
It proves DNS control, not company ownership, which is the strongest claim DNS supports.

Enforcement is not here. It is one `Domain.verified` filter in `api/addresses.py`: an
unverified domain's addresses never reach Redis, so the Go receiver never finds them and
answers `550` through the path it already had.
"""

import uuid

import dns.asyncresolver
import dns.exception
import dns.resolver
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db
from nimbus.api import addresses, security
from nimbus.models import Domain

router = APIRouter(prefix="/v1/domains", tags=["provisioning"])

# Whole-operation budget, not per-packet: dnspython's `lifetime` covers the retries and
# the UDP-truncated-so-retry-over-TCP path together. Short on purpose — this runs inside
# a request, and a resolver that has not answered in 5 seconds is not about to.
DNS_TIMEOUT = 5.0

# ponytail: the lookup runs while this request still holds its pooled database
# connection, so DNS_TIMEOUT seconds of a blackholed resolver x pool_size+max_overflow
# (20) concurrent verify calls would stall unrelated reads. Verify is a rare,
# operator-driven call, so the ceiling is accepted rather than restructured. Move the
# lookup outside the session dependency if this ever becomes self-service.


def _describe(domain: Domain) -> dict:
    """One domain as the reseller needs to see it, including what to put in DNS."""
    challenge = security.domain_challenge(domain.id)
    return {
        "id": str(domain.id),
        "name": domain.name,
        "verified": domain.verified,
        "created_at": domain.created_at,
        # Spelled out rather than documented elsewhere: the commonest way this fails is
        # a reseller putting the record at the apex, or inventing their own host name.
        "dns_record": {
            "type": "TXT",
            "name": f"{security.CHALLENGE_HOST}.{domain.name}",
            "value": challenge,
        },
    }


@router.get("")
async def list_domains(
    reseller_id: uuid.UUID = Depends(security.current_reseller),
    session: AsyncSession = Depends(db.get_session),
):
    """Every domain this reseller owns, with its verification state and challenge.

    The challenge is re-derivable, so unlike the temporary passwords in §9.6 it can be
    read again at any time. That is the point: an order response is seen once, and a
    reseller who lost it must not be stuck with an unverifiable domain.
    """
    rows = (
        await session.scalars(
            select(Domain)
            .where(Domain.reseller_id == reseller_id)
            .order_by(Domain.created_at.desc())
        )
    ).all()
    return {"domains": [_describe(d) for d in rows]}


@router.post("/{domain_id}/verify")
async def verify_domain(
    domain_id: str,
    reseller_id: uuid.UUID = Depends(security.current_reseller),
    session: AsyncSession = Depends(db.get_session),
):
    """Read the TXT record and, if it matches, let this domain receive mail.

    Retryable by design. DNS propagation means the first call routinely fails on a
    correctly configured domain, so the three ways this can go wrong are three different
    status codes rather than one vague failure:

        404  no record yet          -> keep waiting, this is normal for minutes
        409  record present, wrong  -> they published something else, act on it
        503  resolver did not answer-> our problem, not theirs, retry
    """
    domain = await _owned_domain(session, domain_id, reseller_id)
    already = domain.verified

    # Skipping the DNS lookup on re-verify is deliberate. A resolver blip must never be
    # able to un-verify a live domain — nothing in this endpoint ever sets `verified`
    # back to false. Handling a genuine lapse is a sweep, not a side effect of an
    # operator refreshing a page.
    # ponytail: verify once and stay verified. Add a re-check sweep if a tenant ever
    # loses a domain and keeps receiving its mail.
    if not already:
        expected = security.domain_challenge(domain.id)
        host = f"{security.CHALLENGE_HOST}.{domain.name}"
        found = await _lookup_txt(host)

        if not any(security.challenge_matches(v, expected) for v in found):
            raise HTTPException(
                status_code=409,
                detail=(
                    f"found {len(found)} TXT record(s) at {host}, none matching. "
                    "Check the value was pasted whole."
                ),
            )

        domain.verified = True
        # After COMMIT, never before — the same ordering as §9.6. Publishing first and
        # then failing to commit would have the receiver accepting mail for a domain the
        # database still considers unverified.
        await session.commit()

    # OUTSIDE the branch, and that is the point. The publish can fail on its own — a
    # Redis reset, a failover — after the commit has already landed. The reseller then
    # retries, which is the only sensible thing to do, and an early return on
    # `already_verified` would answer 200 and publish NOTHING: verified in Postgres,
    # absent from Redis, every message 550'd until someone restarts the API, with a
    # green response saying it worked.
    #
    # `publish_domain` is SADD-only and idempotent, so running it on every call costs a
    # query and makes this endpoint the repair tool an operator would already reach for.
    published = await addresses.publish_domain(session, domain.id)

    return {**_describe(domain), "already_verified": already, "addresses_published": published}


async def _owned_domain(session: AsyncSession, domain_id: str, reseller_id) -> Domain:
    try:
        parsed = uuid.UUID(domain_id)
    except ValueError:
        # Not a UUID, so it cannot name one of our domains. 404 beats a 500 from
        # Postgres refusing the cast — same reasoning as `orders.get_order`.
        raise HTTPException(status_code=404, detail="No such domain")

    # Filtering on reseller_id is what makes another tenant's domain indistinguishable
    # from one that does not exist. Never look it up by id alone and check ownership
    # afterwards; that leaks which ids are real.
    domain = await session.scalar(
        select(Domain).where(Domain.id == parsed, Domain.reseller_id == reseller_id)
    )
    if domain is None:
        raise HTTPException(status_code=404, detail="No such domain")
    return domain


async def _lookup_txt(host: str) -> list[str]:
    """Every TXT value at `host`. Raises the right HTTPException on every DNS failure.

    The name is not attacker-controlled in any interesting way: it is a domain the
    reseller already registered through `POST /v1/orders`, which validates it against
    `DOMAIN_NAME` first. A DNS lookup is also not a network pivot the way an HTTP fetch
    is, so this needs none of the SSRF machinery `webhooks.py` carries.
    """
    try:
        answer = await dns.asyncresolver.resolve(host, "TXT", lifetime=DNS_TIMEOUT)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        raise HTTPException(
            status_code=404,
            detail=f"no TXT record at {host} yet — DNS changes can take a few minutes",
        )
    except (dns.resolver.LifetimeTimeout, dns.resolver.NoNameservers) as exc:
        # OUR failure, not theirs. A 4xx here would tell a reseller their DNS is wrong
        # when it may well be right, and they would go and break a working record.
        raise HTTPException(
            status_code=503, detail=f"could not reach DNS for {host}: {exc.__class__.__name__}"
        )
    except dns.exception.DNSException as exc:
        # Everything else dnspython can raise, as one clause, because the named four are
        # not the whole set and the rest were reaching the client as 500s. Reachable
        # ones, checked rather than guessed:
        #
        #   NameTooLong  `orders.DOMAIN_NAME` caps each LABEL at 63 characters but puts
        #                no limit on the total, so a 250-character domain registers
        #                fine and prepending `_nimbus-challenge.` pushes it past 255
        #                before a single packet leaves.
        #   NoResolverConfiguration  no usable resolv.conf. Not possible on this laptop;
        #                very possible in block K's container.
        #   YXDOMAIN, LabelTooLong   rare, free to cover here.
        #
        # 503 rather than 4xx for the same reason as above: we cannot tell from here
        # whether the reseller did anything wrong, and guessing blames the wrong party.
        raise HTTPException(
            status_code=503, detail=f"DNS lookup failed for {host}: {exc.__class__.__name__}"
        )

    # A TXT value over 255 bytes arrives as several segments and has to be joined before
    # comparing. Ours is 52 characters so it never splits today — joining anyway costs
    # nothing and stops a longer token from breaking this silently later.
    #
    # `errors="replace"` rather than letting a decode error escape: an unrelated TXT
    # record at this host holding non-ASCII bytes must not turn into a 500. It cannot
    # match the challenge, so it just has to survive being read past.
    return [b"".join(r.strings).decode("ascii", "replace") for r in answer]
