"""The database engine and session factory, shared by every service.

One engine, one session factory, imported by the API, the worker and the scripts. The
alternative — each service building its own — is how two services end up with different
pool sizes, different timeouts, and one of them quietly exhausting Postgres.

Redis lives here too, but only the connection. What goes IN it is business logic and
belongs to whoever owns it: the valid-address cache is the API's, in
`nimbus.api.addresses`.
"""

from collections.abc import AsyncGenerator

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from nimbus.config import settings

# pool_pre_ping costs one cheap round trip per checkout and saves the first query after
# a connection has been dropped by a restart or an idle timeout — which, without it, is
# an error handed to whichever request happened to be unlucky.
engine = create_async_engine(settings.sqlalchemy_url, pool_size=10, pool_pre_ping=True)

# expire_on_commit=False: after commit we still read attributes off the objects we just
# wrote (to build a response, or to log an id). The default would expire them and fire a
# fresh SELECT per attribute, on a session whose transaction has already ended.
Session = async_sessionmaker(engine, expire_on_commit=False)

_redis: aioredis.Redis | None = None


async def connect_redis() -> aioredis.Redis:
    global _redis
    _redis = aioredis.from_url(settings.redis_url, decode_responses=True)
    return _redis


async def close() -> None:
    if _redis is not None:
        await _redis.aclose()
    await engine.dispose()


def redis() -> aioredis.Redis:
    return _redis


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency. One session per request, closed when the request ends."""
    async with Session() as session:
        yield session
