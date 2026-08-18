"""The Nimbus API application — assembly only.

This file creates the app, manages what has to happen on startup and shutdown, and
mounts the routers. No endpoint is defined here, and none should be: docs/HLD.md §10.2
lists 14 of them, and one file holding all of that is a file nobody reads.

    cp backend/.env.example backend/.env      # once
    cd backend
    uv run uvicorn nimbus.api.main:app --reload

Where the rest lives:

    routers/    the HTTP surface, one module per resource
    security.py password hashing, API keys, tokens, the auth dependencies
    addresses.py the valid-address cache the Go SMTP receiver reads at RCPT TO
    webhooks.py outbound calls to a reseller's endpoint
"""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from nimbus import db
from nimbus.api import addresses
from nimbus.api.routers import (
    auth,
    domains,
    health,
    messages,
    orders,
    quota,
    search,
    threads,
)

log = logging.getLogger("nimbus.api")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await db.connect_redis()
    # Rebuilt on every startup, which is what makes Redis safe to treat as a cache: a
    # crash between a commit and its Redis write heals itself here.
    async with db.Session() as session:
        await addresses.refresh(session)
    log.info("connected; address cache rebuilt from Postgres")
    yield
    await db.close()


app = FastAPI(title="Nimbus", version="0.1.0", lifespan=lifespan)

# Each router carries its own prefix and OpenAPI tag, so adding an endpoint means
# editing one router — never this file. Blocks E–G add messages, search and quota.
app.include_router(health.router)
app.include_router(auth.router)
app.include_router(orders.router)
app.include_router(domains.router)
app.include_router(messages.router)
app.include_router(threads.router)
app.include_router(search.router)
app.include_router(quota.router)
