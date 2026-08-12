"""One runnable check for the provisioning rules that only Postgres can prove.

Needs the stack up and the API running:

    docker compose -f infra/docker-compose.yml up -d
    cd backend/api && uv run python -m uvicorn main:app          # in another terminal
    uv run python test_provision_live.py

It creates two throwaway resellers and a throwaway domain, then checks the four
outcomes that the unit checks cannot reach, because each one is a database
constraint doing the work:

    1. a second order on a domain we own      -> 201, mailboxes added
    2. a mailbox name we already have         -> 409, and NOTHING else was created
    3. another reseller's domain              -> 409, worded so it leaks nothing
    4. the same Idempotency-Key twice         -> the first result, replayed

Check 2 is the one worth having. All-or-nothing is a claim about a transaction, and
a transaction that half-commits looks fine until someone counts the rows.
"""

import secrets

import httpx
import pytest
from sqlalchemy import delete, func, select

from nimbus import db
from nimbus.api import security
from nimbus.config import settings
from nimbus.models import Domain, Mailbox, Reseller

# Needs Docker and the API. `pytest -m "not integration"` skips this whole file.
pytestmark = pytest.mark.integration

API = settings.api_base_url
TAG = secrets.token_hex(4)
DOMAIN = f"check-{TAG}.example"


async def make_reseller(session, name: str) -> str:
    key, key_hash = security.new_api_key()
    session.add(Reseller(name=name, api_key_hash=key_hash))
    await session.commit()
    return key


def order(client, key, mailboxes, idem=None):
    return client.post(
        f"{API}/v1/orders",
        json={"domain": DOMAIN, "mailboxes": mailboxes},
        headers={
            "Authorization": f"Bearer {key}",
            "Idempotency-Key": idem or secrets.token_hex(8),
        },
    )


async def test_provisioning_rules() -> None:
    async with db.Session() as session:
        try:
            ours = await make_reseller(session, f"check-owner-{TAG}")
            theirs = await make_reseller(session, f"check-other-{TAG}")

            async with httpx.AsyncClient(timeout=30) as client:
                r = await order(client, ours, ["alice"])
                assert r.status_code == 201, f"first order: {r.status_code} {r.text}"
                print("ok   first order creates the domain")

                r = await order(client, ours, ["bob"])
                assert r.status_code == 201, f"second order: {r.status_code} {r.text}"
                assert r.json()["domain"] == DOMAIN
                print("ok   second order REUSES the domain instead of 409")

                r = await order(client, ours, ["carol", "alice"])
                assert r.status_code == 409, f"duplicate mailbox: {r.status_code} {r.text}"
                print("ok   duplicate mailbox name refused with 409")

                # carol was inserted before alice raised. If the rollback did not
                # happen, she is sitting in the database right now.
                leaked = await session.scalar(
                    select(func.count())
                    .select_from(Mailbox)
                    .join(Domain, Domain.id == Mailbox.domain_id)
                    .where(Domain.name == DOMAIN, Mailbox.local_part == "carol")
                )
                assert leaked == 0, "half-committed: carol exists after a failed order"
                print("ok   the failed order rolled back completely")

                r = await order(client, theirs, ["mallory"])
                assert r.status_code == 409, f"other tenant: {r.status_code} {r.text}"
                assert "another" not in r.text.lower() and "reseller" not in r.text.lower(), \
                    f"409 leaks who owns the domain: {r.text}"
                print("ok   another reseller's domain refused without naming the owner")

                key = secrets.token_hex(8)
                first = await order(client, ours, ["dave"], idem=key)
                again = await order(client, ours, ["dave"], idem=key)
                assert first.status_code == 201, first.text
                assert again.status_code == 201, again.text
                assert again.json().get("replayed") is True, "second call was not a replay"
                assert again.json()["order_id"] == first.json()["order_id"]
                print("ok   the same Idempotency-Key replays instead of creating twice")

            print("\n7 checks passed")
        finally:
            # Domain CASCADEs to its mailboxes; reseller CASCADEs to provision_order.
            # These throwaway tenants never received mail, so nothing RESTRICTs them.
            await session.execute(delete(Domain).where(Domain.name == DOMAIN))
            await session.execute(
                delete(Reseller).where(Reseller.name.like(f"check-%-{TAG}"))
            )
            await session.commit()
    await db.engine.dispose()
