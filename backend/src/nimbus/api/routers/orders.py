"""Provisioning — docs/HLD.md §9.6. Reseller API key, not a mailbox token.

Create a domain and its mailboxes, all of them or none of them. The whole endpoint is
one transaction, and every unusual outcome is a deliberate status code rather than a
surprise: see the 409 table in §9.6.
"""

import re
import secrets
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, Header, HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db
from nimbus.api import addresses, security, webhooks
from nimbus.models import Domain, Mailbox, ProvisionOrder, Reseller

router = APIRouter(prefix="/v1/orders", tags=["provisioning"])

# Input validation at the trust boundary. These strings become email addresses and
# database rows, so they are checked before anything else happens to them.
LOCAL_PART = re.compile(r"^[a-z0-9][a-z0-9._-]{0,62}$")
DOMAIN_NAME = re.compile(
    r"^[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$"
)

PLANS = {
    "5GB": 5 * 1024**3,
    "30GB": 30 * 1024**3,
    "100GB": 100 * 1024**3,
}


class OrderRequest(BaseModel):
    domain: str
    mailboxes: list[str] = Field(min_length=1, max_length=500)
    plan: str = "30GB"

    @field_validator("domain")
    @classmethod
    def _check_domain(cls, v: str) -> str:
        v = v.strip().lower()
        if not DOMAIN_NAME.match(v):
            raise ValueError("not a valid domain name")
        return v

    @field_validator("mailboxes")
    @classmethod
    def _check_mailboxes(cls, v: list[str]) -> list[str]:
        cleaned = []
        for name in v:
            name = name.strip().lower()
            if not LOCAL_PART.match(name):
                raise ValueError(f"not a valid mailbox name: {name!r}")
            cleaned.append(name)
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("the same mailbox name appears twice")
        return cleaned

    @field_validator("plan")
    @classmethod
    def _check_plan(cls, v: str) -> str:
        if v not in PLANS:
            raise ValueError(f"unknown plan, pick one of {sorted(PLANS)}")
        return v


@router.post("", status_code=201)
async def create_order(
    req: OrderRequest,
    background: BackgroundTasks,
    idempotency_key: str = Header(alias="Idempotency-Key"),
    reseller_id: uuid.UUID = Depends(security.current_reseller),
    session: AsyncSession = Depends(db.get_session),
):
    """Create a domain and its mailboxes. All of them, or none of them.

    Sending the same Idempotency-Key twice never creates anything twice — the second
    call replays the first result. Temporary passwords are the one thing NOT replayed:
    they are shown once and only their hashes are stored, so there is nothing to
    replay. That is deliberate, not an oversight.

    Everything below runs in the session's transaction and is committed at the end. Any
    HTTPException raised on the way out closes the session without committing, so a
    half-built order cannot survive.
    """
    replay = await _find_existing_order(session, idempotency_key, reseller_id)
    if replay is not None:
        return replay

    # Generated here so the plaintext exists only in this response. Only hashes are stored.
    passwords = {name: secrets.token_urlsafe(12) for name in req.mailboxes}

    result = await _provision(session, reseller_id, req, passwords, idempotency_key)
    if result is None:
        # Another request holds this key. ON CONFLICT DO NOTHING waits for that
        # transaction to finish before returning nothing, so by now it has either
        # committed — and the result below is real — or rolled back.
        replay = await _find_existing_order(session, idempotency_key, reseller_id)
        if replay is not None:
            return replay
        raise HTTPException(
            status_code=409,
            detail="a request with this Idempotency-Key is still in progress",
        )

    await session.commit()

    # After COMMIT, never before. If we published addresses first and the transaction
    # then rolled back, the receiver would accept mail for mailboxes that do not exist.
    #
    # Only the new addresses, not a full rebuild — see nimbus.api.addresses.add. If this
    # write is lost to a crash, the next API startup rebuilds the set from Postgres and
    # heals it, which is why Redis is a cache here and not the source of truth.
    await addresses.add([m["address"] for m in result["mailboxes"]])

    webhook_url = await session.scalar(
        select(Reseller.webhook_url).where(Reseller.id == reseller_id)
    )
    if webhook_url:
        background.add_task(webhooks.call, webhook_url, result)

    return {**result, "mailboxes": [
        {**m, "temp_password": passwords[m["local_part"]]} for m in result["mailboxes"]
    ]}


@router.get("/{order_id}")
async def get_order(
    order_id: str,
    reseller_id: uuid.UUID = Depends(security.current_reseller),
    session: AsyncSession = Depends(db.get_session),
):
    try:
        parsed = uuid.UUID(order_id)
    except ValueError:
        # Not a UUID at all, so it cannot name one of our orders. 404, not a 500 from
        # Postgres refusing to cast it.
        raise HTTPException(status_code=404, detail="No such order")

    order = await session.scalar(
        select(ProvisionOrder).where(
            ProvisionOrder.id == parsed, ProvisionOrder.reseller_id == reseller_id
        )
    )
    if order is None:
        raise HTTPException(status_code=404, detail="No such order")
    return {
        "order_id": str(order.id),
        "status": order.status,
        "requested": order.payload,
        "result": order.result,
        "created_at": order.created_at,
    }


async def _find_existing_order(session: AsyncSession, idempotency_key: str, reseller_id):
    row = await session.scalar(
        select(ProvisionOrder.result).where(
            ProvisionOrder.idempotency_key == idempotency_key,
            ProvisionOrder.reseller_id == reseller_id,
        )
    )
    if row is None:
        return None
    return {**row, "replayed": True}


async def _provision(
    session: AsyncSession, reseller_id, req: OrderRequest, passwords, idempotency_key
):
    """Returns the created order, or None if another request already holds the key."""
    order_id = await session.scalar(
        pg_insert(ProvisionOrder)
        .values(
            reseller_id=reseller_id,
            idempotency_key=idempotency_key,
            status="pending",
            payload={"domain": req.domain, "mailboxes": req.mailboxes, "plan": req.plan},
        )
        # Reading the outcome from RETURNING rather than catching a unique violation.
        # An exception would abort the whole transaction and would also tie us to how
        # SQLAlchemy happens to wrap asyncpg's error type — a detail we should not
        # depend on for ordinary control flow.
        .on_conflict_do_nothing(index_elements=[ProvisionOrder.idempotency_key])
        .returning(ProvisionOrder.id)
    )
    if order_id is None:
        return None

    # A reseller ordering a SECOND batch of mailboxes on a domain they already own is
    # normal business, not an error. Insert if new, otherwise reuse — but only if ours.
    domain_id = await session.scalar(
        pg_insert(Domain)
        .values(reseller_id=reseller_id, name=req.domain)
        .on_conflict_do_nothing(index_elements=[Domain.name])
        .returning(Domain.id)
    )
    if domain_id is None:
        # Nothing came back, so the domain exists — for somebody. Filtering by
        # reseller_id is what separates "ours already" from "another tenant's".
        domain_id = await session.scalar(
            select(Domain.id).where(
                Domain.name == req.domain, Domain.reseller_id == reseller_id
            )
        )
    if domain_id is None:
        # Deliberately vague. "Registered to another reseller" would confirm which
        # domains our other tenants own to anyone who can guess a name.
        raise HTTPException(status_code=409, detail="domain not available")

    created = []
    for name in req.mailboxes:
        mailbox_id = await session.scalar(
            pg_insert(Mailbox)
            .values(
                domain_id=domain_id,
                local_part=name,
                password_hash=security.hash_password(passwords[name]),
                quota_bytes=PLANS[req.plan],
                plan=req.plan,
            )
            .on_conflict_do_nothing(index_elements=[Mailbox.domain_id, Mailbox.local_part])
            .returning(Mailbox.id)
        )
        # All of them or none of them. Silently skipping the clash would return a temp
        # password for a mailbox whose password we did not actually change.
        if mailbox_id is None:
            raise HTTPException(
                status_code=409, detail=f"mailbox already exists: {name}@{req.domain}"
            )
        created.append({
            "id": str(mailbox_id),
            "local_part": name,
            "address": f"{name}@{req.domain}",
        })

    result = {
        "order_id": str(order_id),
        "domain": req.domain,
        "domain_id": str(domain_id),
        "plan": req.plan,
        "mailboxes": created,
    }

    await session.execute(
        ProvisionOrder.__table__.update()
        .where(ProvisionOrder.id == order_id)
        .values(status="done", result=result)
    )
    return result
