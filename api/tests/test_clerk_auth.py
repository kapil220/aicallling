import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException

ISSUER = "https://test.clerk.accounts.dev"


@pytest.fixture(scope="module")
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _make_token(rsa_key, *, iss=ISSUER, exp_delta=3600, sub="user_abc", **extra):
    payload = {
        "iss": iss,
        "sub": sub,
        "exp": int(time.time()) + exp_delta,
        "iat": int(time.time()),
        **extra,
    }
    return jwt.encode(payload, rsa_key, algorithm="RS256")


@pytest.fixture
def patched(monkeypatch, rsa_key):
    from api.services.auth import clerk_auth

    monkeypatch.setattr(clerk_auth, "CLERK_ISSUER", ISSUER)
    monkeypatch.setattr(
        clerk_auth, "_get_signing_key", lambda token: rsa_key.public_key()
    )
    return clerk_auth


async def test_valid_token_returns_claims(patched, rsa_key):
    claims = await patched.verify_clerk_token(
        f"Bearer {_make_token(rsa_key, email='a@b.com')}"
    )
    assert claims["sub"] == "user_abc"
    assert claims["email"] == "a@b.com"


async def test_missing_header_401(patched):
    with pytest.raises(HTTPException) as exc:
        await patched.verify_clerk_token(None)
    assert exc.value.status_code == 401


async def test_expired_token_401(patched, rsa_key):
    with pytest.raises(HTTPException) as exc:
        await patched.verify_clerk_token(
            f"Bearer {_make_token(rsa_key, exp_delta=-60)}"
        )
    assert exc.value.status_code == 401


async def test_wrong_issuer_401(patched, rsa_key):
    with pytest.raises(HTTPException) as exc:
        await patched.verify_clerk_token(
            f"Bearer {_make_token(rsa_key, iss='https://evil.example.com')}"
        )
    assert exc.value.status_code == 401
