"""One runnable check for security.py. No database, no server, no framework.

    cd backend/api
    python test_security.py

Auth is the piece where a quiet bug becomes a security hole, so it gets the check.
"""

import base64
import json
import time

import jwt

from nimbus.api import security
from nimbus.config import settings


def test_passwords_round_trip():
    stored = security.hash_password("correct horse battery staple")
    assert stored != "correct horse battery staple", "password stored in the clear"
    assert security.verify_password("correct horse battery staple", stored)
    assert not security.verify_password("wrong horse battery staple", stored)


def test_bad_hash_does_not_crash():
    # A corrupt row must fail closed, not raise and 500 the login endpoint.
    assert not security.verify_password("anything", "not-even-a-hash")


def test_same_password_hashes_differently():
    # Argon2 salts every hash. Two identical passwords must not look identical.
    assert security.hash_password("same") != security.hash_password("same")


def test_api_key_round_trip():
    key, stored = security.new_api_key()
    assert key != stored, "API key stored in the clear"
    assert security.hash_api_key(key) == stored
    another, _ = security.new_api_key()
    assert another != key, "API keys are not unique"


def test_token_round_trip():
    token = security.make_token("abc-123")
    assert security.read_token(token) == "abc-123"


def test_tampered_token_rejected():
    token = security.make_token("abc-123")
    assert security.read_token(token + "x") is None
    assert security.read_token("garbage") is None
    assert security.read_token("") is None


def test_unsigned_token_rejected():
    """The alg=none attack.

    A forged token claiming it needs no signature must be rejected. This is why
    read_token passes algorithms=["HS256"] explicitly instead of trusting the header.
    """
    def b64(obj):
        raw = json.dumps(obj).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    forged = f'{b64({"alg": "none", "typ": "JWT"})}.{b64({"sub": "victim"})}.'
    assert security.read_token(forged) is None


def test_expired_token_rejected():
    expired = jwt.encode(
        {"sub": "abc-123", "exp": int(time.time()) - 10},
        settings.jwt_secret,
        algorithm="HS256",
    )
    assert security.read_token(expired) is None


def test_token_from_another_secret_rejected():
    foreign = jwt.encode(
        {"sub": "abc-123", "exp": int(time.time()) + 60},
        "a-different-secret-of-at-least-32-bytes",
        algorithm="HS256",
    )
    assert security.read_token(foreign) is None
