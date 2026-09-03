"""`require_user` FastAPI dependency - the public-user-session counterpart
to `require_admin` (ADR-020, approved).

Deliberately a separate file from `app/core/admin_auth.py`, with ZERO
imports from it (and vice versa) - the isolation ADR-020's five-boundary
table requires: this reads the `user_session` cookie and verifies it via
`app/services/auth/user_token_service.py` (own secret, own `user_access`
token-type claim); `require_admin` reads `admin_session` and verifies via
`app/services/auth/token_service.py` (own secret, own `admin_access`
claim). Neither function calls the other, neither imports the other's
token-service module, and the two cookie names/collections/secrets never
appear in the same file. A forged/leaked admin session cookie cannot
satisfy this dependency, and a forged/leaked user session cookie cannot
satisfy `require_admin` - not because of any runtime check cross-referencing
the two, but because the two code paths never share a value or line of code
that could make that possible.

No `/users/*` protected route exists yet in Round 1 (no dashboard/profile
endpoints - ADR-020 explicitly excludes those). This dependency is provided
now so a future protected user route has it ready, the same way
`require_admin` existed before every `/admin/*` route that uses it today.
"""
import logging

from fastapi import Request

from app.services.auth.user_token_service import decode_user_access_token
from app.shared.responses import api_error

logger = logging.getLogger(__name__)

# Shared by both this dependency (reading the cookie) and
# app/routers/auth.py (setting/clearing it on signup/login/logout) - defined
# once here rather than duplicated in both places. Deliberately distinct
# from admin_auth.py's ADMIN_COOKIE_NAME (ADR-020 isolation table).
USER_COOKIE_NAME = "user_session"


async def require_user(request: Request) -> dict:
    """Raises a 401 (standard envelope, Handbook Part C.5) if the request has
    no valid, unexpired user session cookie. On success, returns a small
    dict identifying the caller (`{"email": ...}`) for any route that wants
    it - never the raw token itself.
    """
    token = request.cookies.get(USER_COOKIE_NAME)
    if not token:
        logger.warning("require_user: no user session cookie present")
        raise api_error(401, "Not authenticated", "USER_AUTH_REQUIRED")

    payload = decode_user_access_token(token)
    if payload is None:
        logger.warning("require_user: user session cookie invalid or expired")
        raise api_error(401, "Session invalid or expired", "USER_AUTH_INVALID")

    return {"email": payload.sub}
