"""Liveness. No prefix and no auth — load balancers and `docker compose` read this."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(session: AsyncSession = Depends(db.get_session)):
    """Proves the API can actually reach both stores, not just that it booted.

    A health check that only returns 200 reports healthy while Postgres is unreachable,
    which is worse than no health check: the load balancer keeps sending traffic to a
    process that cannot serve any of it.
    """
    await session.scalar(select(1))
    await db.redis().ping()
    return {"status": "ok"}
