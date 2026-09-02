"""`auth` module HTTP surface - brand-new human-admin login/logout, entirely
separate from `app/core/security.py::verify_api_key`'s existing
service-to-service `x-api-key` mechanism (untouched by this feature).

No `/auth/register` or `/admin/signup` route exists here or anywhere else
in this codebase - accounts only come from `backend/scripts/seed_admin.py`.

Session is a JWT carried in an httpOnly cookie (never a bearer header,
never returned in the JSON body) - see `app/core/admin_auth.py::require_admin`
for how any future `/admin/*` route (the upcoming homepage-sections CMS)
verifies it.
"""
import logging

from fastapi import APIRouter, Request, Response

from app.core.admin_auth import ADMIN_COOKIE_NAME
from app.core.config import settings
from app.core.rate_limiter import limiter
from app.schemas.auth import AdminLoginRequest
from app.services.auth.login_service import LoginResult, attempt_login
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])

# Same 401 body regardless of *why* login failed (unknown email, wrong
# password, or a locked account) - this feature's explicit
# no-user-enumeration requirement.
_INVALID_CREDENTIALS_MESSAGE = "Invalid email or password"


@router.post("/login", summary="Admin login - sets an httpOnly session cookie")
@limiter.limit("10/minute")
async def login(request: Request, response: Response, body: AdminLoginRequest):
    result, token = await attempt_login(body.email, body.password)

    if result != LoginResult.ALLOWED:
        # LOCKED gets a distinct server-side log line (already emitted in
        # login_service) but the exact same client-facing 401 as DENIED -
        # never let the response shape hint that the account exists/is
        # locked vs. simply wrong credentials.
        raise api_error(401, _INVALID_CREDENTIALS_MESSAGE, "INVALID_CREDENTIALS")

    # httpOnly + Secure + SameSite=Strict per spec - never readable from JS,
    # never sent on a cross-site request. `max_age` in seconds, matching
    # the JWT's own expiry so the cookie doesn't outlive the token it
    # carries.
    response.set_cookie(
        key=ADMIN_COOKIE_NAME,
        value=token,
        max_age=settings.admin_jwt_expires_hours * 3600,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    logger.info("Admin login succeeded, session cookie set")
    return envelope(True, "Login successful", data={"email": body.email})


@router.post("/logout", summary="Admin logout - clears the session cookie")
async def logout(response: Response):
    # Intentionally does not depend on require_admin: a client with an
    # already-expired/invalid cookie must still be able to clear it
    # client-side, so this always succeeds.
    response.delete_cookie(key=ADMIN_COOKIE_NAME, path="/", samesite="strict", secure=True, httponly=True)
    logger.info("Admin logout, session cookie cleared")
    return envelope(True, "Logged out", data=None)
