"""`require_admin` FastAPI dependency - the admin-session counterpart to
`verify_api_key` in this same directory.

Deliberately a separate file/function from `app/core/security.py`
(untouched by this work): `verify_api_key` authenticates a service-to-service
`x-api-key` header against the `api_keys` collection; this authenticates a
human admin's httpOnly-cookie JWT against `app/services/auth/token_service.py`.
Nothing here reads `x-api-key` and nothing in `verify_api_key` reads this
cookie - the two mechanisms never share code or state.

Any future `/admin/*` route (the upcoming homepage-sections CMS) depends on
this the same way existing tool routers depend on `verify_api_key`:
`Depends(require_admin)`.
"""
import logging

from fastapi import Request

from app.services.auth.token_service import decode_admin_access_token
from app.shared.responses import api_error

logger = logging.getLogger(__name__)

# Shared by both this dependency (reading the cookie) and
# app/routers/auth.py (setting/clearing it on login/logout) - defined once
# here rather than duplicated in both places.
ADMIN_COOKIE_NAME = "admin_session"


async def require_admin(request: Request) -> dict:
    """Raises a 401 (standard envelope, Handbook Part C.5) if the request has
    no valid, unexpired admin session cookie. On success, returns a small
    dict identifying the caller (`{"email": ...}`) for any route that wants
    it - never the raw token itself.
    """
    token = request.cookies.get(ADMIN_COOKIE_NAME)
    if not token:
        logger.warning("require_admin: no admin session cookie present")
        raise api_error(401, "Not authenticated", "ADMIN_AUTH_REQUIRED")

    payload = decode_admin_access_token(token)
    if payload is None:
        logger.warning("require_admin: admin session cookie invalid or expired")
        raise api_error(401, "Session invalid or expired", "ADMIN_AUTH_INVALID")

    return {"email": payload.sub}
