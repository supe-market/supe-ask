from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
from fastapi import HTTPException, Request, Response

from .config import settings
from .db import db


@dataclass(frozen=True)
class AuthUser:
    id: str
    user_type: str
    user_role: str | None
    tenant_id: str
    raw_token: str


def _resolve_internal_tenant_id(raw_tenant_id: Any) -> str | None:
    tenant_value = str(raw_tenant_id or "").strip()
    if not tenant_value:
        return None

    if tenant_value.isdigit():
        row = db.fetch_one("select id from tenants where id = %s limit 1", [int(tenant_value)])
        return str(row["id"]) if row else None

    row = db.fetch_one("select id from tenants where tenant_code = %s limit 1", [tenant_value])
    return str(row["id"]) if row else None


def _auth_headers(token: str | None = None) -> dict[str, str]:
    headers: dict[str, str] = {}
    if settings.ums_auth_param:
        headers["Authorization"] = settings.ums_auth_param
    if token:
        headers["x-session-token"] = token
    return headers


async def verify_session_token(token: str) -> AuthUser | None:
    url = f"{settings.ums_auth_url}/ums/ts/verifyToken/supe"
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.get(url, headers=_auth_headers(token))
            data = response.json()
    except Exception:
        return None

    if data.get("status") != "Success":
        return None
    payload = data.get("responseBody") or {}
    tenant_id = _resolve_internal_tenant_id(payload.get("supeTenantId"))
    if not tenant_id:
        return None
    return AuthUser(
        id=str(payload.get("entityId", "")),
        user_type=str(payload.get("userType", "")),
        user_role=payload.get("userRole"),
        tenant_id=str(tenant_id),
        raw_token=token,
    )


async def verify_oauth_code(oauth_code: str) -> tuple[str, AuthUser] | None:
    url = f"{settings.ums_auth_url}/oauth2/token/supe"
    body = {"grantType": "authorization_code", "code": oauth_code}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            response = await client.post(url, json=body, headers=_auth_headers())
            data = response.json()
    except Exception:
        return None

    if data.get("status") != "Success":
        return None
    payload = data.get("responseBody") or {}
    token = payload.get("accessToken")
    tenant_id = _resolve_internal_tenant_id(payload.get("supeTenantId"))
    if not token or not tenant_id:
        return None
    return (
        token,
        AuthUser(
            id=str(payload.get("entityId", "")),
            user_type=str(payload.get("userType", "")),
            user_role=payload.get("userRole"),
            tenant_id=str(tenant_id),
            raw_token=str(token),
        ),
    )


FRESH_MARKER_COOKIE = "AUTH_FRESH_SUPE_ASK"


def _cookie_options(request: Request) -> dict[str, Any]:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    is_https = request.url.scheme == "https" or "https" in forwarded_proto
    secure = settings.ask_cookie_secure or is_https or settings.node_env == "production"
    same_site = "none" if secure and settings.ask_cookie_same_site == "lax" else settings.ask_cookie_same_site
    return {
        "httponly": True,
        "secure": secure,
        "samesite": same_site,
        "path": "/",
        "max_age": settings.session_ttl_seconds,
    }


def _marker_cookie_options(request: Request) -> dict[str, Any]:
    # Same security flags as the auth cookie, but max_age is half the TTL so
    # the marker disappears at the halfway point and triggers a refresh.
    opts = _cookie_options(request)
    opts["max_age"] = settings.session_ttl_seconds // 2
    return opts


def set_auth_cookie(request: Request, response: Response, token: str) -> None:
    response.set_cookie(settings.ask_cookie_name, token, **_cookie_options(request))
    response.set_cookie(FRESH_MARKER_COOKIE, "1", **_marker_cookie_options(request))


def clear_auth_cookie(request: Request, response: Response) -> None:
    response.delete_cookie(settings.ask_cookie_name, path="/")
    response.delete_cookie(FRESH_MARKER_COOKIE, path="/")


def _is_session_fresh(request: Request) -> bool:
    return bool(request.cookies.get(FRESH_MARKER_COOKIE))


def extract_request_token(request: Request) -> str | None:
    cookie_token = request.cookies.get(settings.ask_cookie_name)
    if cookie_token:
        return cookie_token
    header_token = request.headers.get("x-session-token")
    return header_token.strip() if header_token else None


async def require_auth(request: Request, response: Response) -> AuthUser:
    token = extract_request_token(request)
    if not token:
        raise HTTPException(status_code=403, detail="No token present")
    user = await verify_session_token(token)
    if not user:
        raise HTTPException(status_code=403, detail="Token is invalid")
    # Sliding window with halfway-point refresh: only re-issue the cookie
    # once the short-lived marker has expired. Avoids a Set-Cookie header
    # on every request.
    if not _is_session_fresh(request):
        set_auth_cookie(request, response, token)
    return user
