"""Credentials and tokens for the two callers in docs/HLD.md §10.1.

    mailbox user  ->  password  ->  JWT, 24h, carries mailbox_id
    reseller      ->  API key   ->  carries reseller_id

The rule that matters: the caller's id always comes from the credential, never from
the URL or the body. Every query filters on it. One missed filter is a cross-tenant
data leak, which is the worst bug this system can have.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from nimbus import db
from nimbus.config import settings
from nimbus.models import Mailbox, Reseller

_hasher = PasswordHasher()
_bearer = HTTPBearer(auto_error=False)


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Not authenticated",
        headers={"WWW-Authenticate": "Bearer"},
    )


# --------------------------------------------------------------------------
# Passwords — for humans
# --------------------------------------------------------------------------

def hash_password(plain: str) -> str:
    return _hasher.hash(plain)


def verify_password(plain: str, stored_hash: str) -> bool:
    try:
        return _hasher.verify(stored_hash, plain)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


# --------------------------------------------------------------------------
# API keys — for machines
# --------------------------------------------------------------------------
#
# SHA-256 here, not Argon2, and that is deliberate.
#
# Argon2 is slow ON PURPOSE. That is exactly right for human passwords: they are
# short, guessable, and verified once per login. It is exactly wrong for API keys:
# 32 random bytes are not guessable by brute force, and the key is sent on EVERY
# request. Argon2 there would be a self-inflicted rate limit buying no security.

def new_api_key() -> tuple[str, str]:
    """Returns (key shown to the reseller once, hash we store)."""
    key = secrets.token_urlsafe(32)
    return key, hash_api_key(key)


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


# --------------------------------------------------------------------------
# Tokens
# --------------------------------------------------------------------------

def make_token(mailbox_id: str) -> str:
    now = datetime.now(timezone.utc)
    return jwt.encode(
        {
            "sub": str(mailbox_id),
            "iat": now,
            "exp": now + timedelta(hours=settings.jwt_ttl_hours),
        },
        settings.jwt_secret,
        algorithm="HS256",
    )


def read_token(token: str) -> str | None:
    """Returns the mailbox_id, or None if the token is bad in any way."""
    try:
        # Naming the algorithm explicitly is not optional. Without it a forged token
        # carrying alg="none" would be accepted as valid.
        payload = jwt.decode(token, settings.jwt_secret, algorithms=["HS256"])
        return payload["sub"]
    except (jwt.InvalidTokenError, KeyError):
        return None


# --------------------------------------------------------------------------
# FastAPI dependencies — put one of these on every endpoint
# --------------------------------------------------------------------------

async def current_mailbox(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(db.get_session),
) -> uuid.UUID:
    if creds is None:
        raise _unauthorized()
    mailbox_id = read_token(creds.credentials)
    if mailbox_id is None:
        raise _unauthorized()
    try:
        # We minted this claim, so it is always a UUID — but this is a trust boundary,
        # and a bad value here would surface as a 500 instead of a 401.
        mailbox_id = uuid.UUID(mailbox_id)
    except ValueError:
        raise _unauthorized()
    # A JWT is stateless: valid until it expires, no matter what happened since. Delete
    # a mailbox and its holder keeps reading mail for up to 24 more hours — for a
    # business email product, that is an ex-employee with a live inbox.
    #
    # One lookup on the primary key closes it. It costs a sub-millisecond indexed read
    # per request, and every authenticated endpoint hits Postgres anyway. The trade-off
    # is real and taken deliberately: the token is no longer verifiable offline.
    if not await session.scalar(select(Mailbox.id).where(Mailbox.id == mailbox_id)):
        raise _unauthorized()
    return mailbox_id


async def current_reseller(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    session: AsyncSession = Depends(db.get_session),
) -> uuid.UUID:
    if creds is None:
        raise _unauthorized()
    reseller_id = await session.scalar(
        select(Reseller.id).where(Reseller.api_key_hash == hash_api_key(creds.credentials))
    )
    if reseller_id is None:
        raise _unauthorized()
    return reseller_id
