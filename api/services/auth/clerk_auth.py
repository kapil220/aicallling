"""Clerk session-token verification (AUTH_PROVIDER=clerk).

Clerk issues short-lived RS256 session JWTs. We verify them locally against
Clerk's JWKS (cached by PyJWKClient) — no network round-trip per request.
"""

from functools import lru_cache

import jwt
from fastapi import HTTPException
from jwt import PyJWKClient

from api.constants import CLERK_ISSUER


@lru_cache(maxsize=1)
def _jwks_client() -> PyJWKClient:
    return PyJWKClient(
        f"{CLERK_ISSUER}/.well-known/jwks.json", cache_keys=True, lifespan=3600
    )


def _get_signing_key(token: str):
    """Seam for tests: resolve the public key for this token's `kid`."""
    return _jwks_client().get_signing_key_from_jwt(token).key


async def verify_clerk_token(authorization: str | None) -> dict:
    if not authorization:
        raise HTTPException(status_code=401, detail="Authorization header required")
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Invalid authorization token")

    try:
        return jwt.decode(
            token,
            _get_signing_key(token),
            algorithms=["RS256"],
            issuer=CLERK_ISSUER,
            options={"require": ["exp", "iat", "sub"], "verify_aud": False},
            leeway=10,
        )
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
