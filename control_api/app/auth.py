"""Authentication for project backends (PROJECT_TOKEN) and the owner (Supabase JWT)."""

import hashlib
import hmac
from functools import lru_cache

import jwt
from fastapi import Depends, Header, HTTPException, Path, status

from .db import conn
from .settings import Settings, get_settings


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def hash_visitor(visitor_id: str, settings: Settings) -> str:
    return hmac.new(settings.visitor_hash_key.encode(), visitor_id.encode(), hashlib.sha256).hexdigest()[:32]


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing bearer token")
    return authorization.split(" ", 1)[1].strip()


async def require_project(
    project_id: str = Path(...),
    authorization: str | None = Header(default=None),
) -> str:
    """The token must belong to the project named in the URL; one project cannot touch another."""
    token = _bearer(authorization)
    async with conn() as c:
        row = await (await c.execute(
            "select project_id from project_tokens where token_sha256 = %s and revoked_at is null",
            (sha256_hex(token),),
        )).fetchone()
    if not row or not hmac.compare_digest(row["project_id"], project_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "invalid project token")
    return project_id


@lru_cache
def _jwks_client(supabase_url: str) -> jwt.PyJWKClient:
    return jwt.PyJWKClient(f"{supabase_url.rstrip('/')}/auth/v1/.well-known/jwks.json")


def _decode_owner_jwt(token: str, settings: Settings) -> dict:
    opts = {"audience": "authenticated"}
    if settings.supabase_jwt_secret:
        return jwt.decode(token, settings.supabase_jwt_secret, algorithms=["HS256"], **opts)
    if settings.supabase_url:
        key = _jwks_client(settings.supabase_url).get_signing_key_from_jwt(token).key
        return jwt.decode(token, key, algorithms=["RS256", "ES256"], **opts)
    raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "owner auth not configured")


async def require_owner(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> str:
    token = _bearer(authorization)
    try:
        claims = _decode_owner_jwt(token, settings)
    except jwt.PyJWTError:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid session")
    email = (claims.get("email") or "").lower()
    if not email:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not an owner")
    if email in {e.lower() for e in settings.owner_emails}:
        return email
    async with conn() as c:
        row = await (await c.execute("select 1 from owners where lower(email) = %s", (email,))).fetchone()
    if not row:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "not an owner")
    return email


def require_cron(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings),
) -> None:
    if not settings.cron_secret or not hmac.compare_digest(_bearer(authorization), settings.cron_secret):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "forbidden")
