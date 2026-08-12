"""Mailbox login — docs/HLD.md §10.1.

The password-hashing and token machinery lives in `nimbus.api.security`. This module is
only the HTTP surface over it.
"""

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db
from nimbus.api import security
from nimbus.models import Domain, Mailbox

router = APIRouter(prefix="/v1/auth", tags=["auth"])


class LoginRequest(BaseModel):
    address: str
    password: str


@router.post("/login")
async def login(req: LoginRequest, session: AsyncSession = Depends(db.get_session)):
    local_part, _, domain_name = req.address.strip().lower().partition("@")

    row = (
        await session.execute(
            select(Mailbox.id, Mailbox.password_hash)
            .join(Domain, Domain.id == Mailbox.domain_id)
            .where(Mailbox.local_part == local_part, Domain.name == domain_name)
        )
    ).first()

    # One message for "no such mailbox" and for "wrong password". Telling them apart
    # would let anyone enumerate which addresses exist.
    if row is None or row.password_hash is None:
        raise HTTPException(status_code=401, detail="Invalid address or password")
    if not security.verify_password(req.password, row.password_hash):
        raise HTTPException(status_code=401, detail="Invalid address or password")

    return {"token": security.make_token(str(row.id)), "expires_in_hours": 24}
