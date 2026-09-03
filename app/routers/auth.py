"""`auth` module HTTP surface - brand-new human-admin login/logout, entirely
separate from `app/core/security.py::verify_api_key`'s existing
service-to-service `x-api-key` mechanism (untouched by this feature).

No `/auth/register` or `/admin/signup` route exists here or anywhere else
in this codebase - admin accounts only come from
`backend/scripts/seed_admin.py`.

Session is a JWT carried in an httpOnly cookie (never a bearer header,
never returned in the JSON body) - see `app/core/admin_auth.py::require_admin`
for how any future `/admin/*` route (the upcoming homepage-sections CMS)
verifies it.

Public User Auth (ADR-020, approved) routes live in this SAME router below
the admin routes - `auth` owns both identity systems (ADR-020's Option A:
extend `auth`, don't fork a new module), kept internally isolated per that
ADR's five-boundary table (own collection/cookie/secret/token-type/guard -
see `app/core/user_auth.py`, `app/services/auth/user_*.py`). Unlike admin,
`POST /auth/signup` IS a real public self-registration endpoint - this is
the first one in the codebase. The rest of the new surface is namespaced
under `/auth/users/...` so it can never collide with the admin routes
above (`/auth/login`, `/auth/logout`).

Neither this router file, `app/core/user_auth.py`, nor
`app/services/auth/user_*.py` import anything from `app/core/admin_auth.py`
or `app/services/auth/{token_service,login_service,admin_user_service}.py`
(or vice versa) - the isolation is structural, not just a naming
convention.
"""
import logging
import secrets

from fastapi import APIRouter, Request, Response
from pydantic import SecretStr

from app.core.admin_auth import ADMIN_COOKIE_NAME
from app.core.config import settings
from app.core.rate_limiter import limiter
from app.core.user_auth import USER_COOKIE_NAME
from app.schemas.auth import (
    AdminLoginRequest,
    PasswordResetConfirmRequest,
    PasswordResetRequestRequest,
    UserLoginRequest,
    UserSignupRequest,
)
from app.services.auth import user_service
from app.services.auth.login_service import LoginResult, attempt_login
from app.services.auth.password_service import hash_password
from app.services.auth.user_login_service import UserLoginResult, attempt_user_login
from app.services.auth.user_token_service import create_user_access_token
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Auth"])

# Same 401 body regardless of *why* login failed (unknown email, wrong
# password, or a locked account) - this feature's explicit
# no-user-enumeration requirement.
_INVALID_CREDENTIALS_MESSAGE = "Invalid email or password"
_USER_INVALID_CREDENTIALS_MESSAGE = "Invalid email or password"


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


# =========================================================================
# Public User Auth (ADR-020, approved) - everything below this line is the
# second, isolated identity system this module owns. See this file's
# module docstring for the isolation guarantee.
# =========================================================================


@router.post("/signup", summary="Public user signup - creates an account and auto-logs-in")
@limiter.limit("5/hour")
async def signup(request: Request, response: Response, body: UserSignupRequest):
    """First public self-registration endpoint in this codebase. Rate
    limited (5/hour/IP) + honeypot per ADR-020 Trade-offs (no CAPTCHA in
    Round 1). Auto-logs-in on success (sets `user_session`, same as a real
    login) - ADR-020's Decision section calls this out explicitly.
    """
    if body.website:
        # Honeypot tripped: silently pretend success without ever creating
        # an account - but the response must be genuinely indistinguishable
        # from a real signup's, not just same-status/same-body
        # (security-reviewer finding). Two prior signals let a bot detect
        # rejection: no Set-Cookie header, and a much faster response
        # (skipping hash_password()'s ~100-300ms bcrypt call and the
        # create_user Mongo round-trip). Fix both below - same reasoning as
        # verify_password_constant_time_dummy in
        # password_service.py/login_service.py for the identical class of
        # timing-parity problem on login's "unknown email" branch.
        logger.warning("Signup honeypot triggered, silently rejecting")

        # Burn comparable bcrypt time to a real signup - actually hash the
        # submitted password (never persisted, never logged) rather than a
        # cheaper simulation, so the cost is exactly as real as the
        # non-honeypot branch's.
        hash_password(body.password)
        # Burn a comparable Mongo round-trip to create_user's initial
        # find-before-insert lookup. Read-only - never writes a document.
        await user_service.get_user_by_email(body.email)

        # Set a structurally-identical cookie (same name/flags/max_age) so
        # a bot can't use Set-Cookie's presence/absence as a signal either.
        # The value is opaque random data, NOT a valid signed session token
        # - decode_user_access_token can never accept it (fails signature/
        # format checks outright), so this never actually authenticates the
        # caller. Response-shape parity only, never a real session.
        response.set_cookie(
            key=USER_COOKIE_NAME,
            value=secrets.token_urlsafe(64),
            max_age=settings.user_jwt_expires_hours * 3600,
            httponly=True,
            secure=True,
            samesite="strict",
            path="/",
        )
        return envelope(True, "Account created", data={"email": body.email})

    try:
        password_hash = hash_password(body.password)
        user = await user_service.create_user(body.email, password_hash)
    except ValueError:
        # Ordinary registration-form UX, not a user-enumeration issue: the
        # entire point of this endpoint is "does this email already have an
        # account", unlike login/password-reset which must never confirm
        # that (see the identical-401 / always-200 comments further below).
        logger.info("Signup rejected: email already registered")
        raise api_error(409, "An account with this email already exists", "EMAIL_TAKEN")

    token = create_user_access_token(user.email)
    # Same httpOnly + Secure + SameSite=Strict cookie pattern as admin
    # login above.
    response.set_cookie(
        key=USER_COOKIE_NAME,
        value=token,
        max_age=settings.user_jwt_expires_hours * 3600,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    logger.info("User signup succeeded, session cookie set")
    return envelope(True, "Account created", data={"email": user.email})


@router.post("/users/login", summary="User login - sets an httpOnly session cookie")
@limiter.limit("10/minute")
async def user_login(request: Request, response: Response, body: UserLoginRequest):
    result, token = await attempt_user_login(body.email, body.password)

    if result != UserLoginResult.ALLOWED:
        # Same identical-body posture as admin login above - DENIED/LOCKED
        # both collapse to this one 401.
        raise api_error(401, _USER_INVALID_CREDENTIALS_MESSAGE, "INVALID_CREDENTIALS")

    response.set_cookie(
        key=USER_COOKIE_NAME,
        value=token,
        max_age=settings.user_jwt_expires_hours * 3600,
        httponly=True,
        secure=True,
        samesite="strict",
        path="/",
    )
    logger.info("User login succeeded, session cookie set")
    return envelope(True, "Login successful", data={"email": body.email})


@router.post("/users/logout", summary="User logout - clears the session cookie")
async def user_logout(response: Response):
    # Same reasoning as admin logout above: always succeeds, even with a
    # stale/invalid cookie, so a client can always clear its local state.
    response.delete_cookie(key=USER_COOKIE_NAME, path="/", samesite="strict", secure=True, httponly=True)
    logger.info("User logout, session cookie cleared")
    return envelope(True, "Logged out", data=None)


@router.post(
    "/users/password-reset/request",
    summary="Request a password reset email (Tier 1 - dispatch is a Tier 2 job)",
)
@limiter.limit("5/hour")
async def request_password_reset(request: Request, body: PasswordResetRequestRequest):
    """Mints + stores a one-time reset token (if the email exists) and
    enqueues the actual Resend send as a Tier 2 ARQ job
    (`app/worker.py::send_password_reset_email`) - ADR-020's explicit Tier
    split, so a slow/unavailable Resend never blocks or fails this
    synchronous endpoint. Always returns 200 regardless of whether the
    email exists (this feature's no-user-enumeration requirement, same
    posture as login).
    """
    raw_token = await user_service.issue_password_reset_token(body.email)

    if raw_token is not None:
        try:
            # Wrapped in SecretStr (not a plain str) so that arq's own
            # default job-argument logging (`arq.worker.Worker.run_job`,
            # via `args_to_string`'s `repr()` over every positional arg)
            # logs `SecretStr('**********')` instead of the raw token -
            # see app/worker.py::send_password_reset_email's docstring and
            # tests/test_worker_password_reset_secret_leak.py for the
            # regression this closes. Our own code never logged this value
            # either way; the leak was entirely from arq's internals.
            await request.app.state.arq_redis.enqueue_job(
                "send_password_reset_email", body.email, SecretStr(raw_token)
            )
            logger.info("Enqueued password-reset email job")
        except Exception as e:
            # A queue outage must delay, not break, this response (ADR-020
            # Trade-offs) - log and fall through to the same 200 below
            # either way. The user just won't get the email until the
            # queue recovers and a fresh request is made.
            logger.exception("Failed to enqueue password-reset email job: %s", str(e))
    else:
        logger.info("Password reset requested for an email with no matching account")

    return envelope(
        True, "If that email address has an account, a password reset link has been sent", data=None
    )


@router.post("/users/password-reset/confirm", summary="Confirm a password reset with a one-time token")
@limiter.limit("20/hour")
async def confirm_password_reset(request: Request, body: PasswordResetConfirmRequest):
    """Validates the token (hash match + not expired, application-layer
    check - see `user_service.get_user_by_valid_reset_token`), sets the new
    password, and invalidates the token (single-use)."""
    user = await user_service.get_user_by_valid_reset_token(body.token)
    if user is None:
        raise api_error(
            400, "This password reset link is invalid or has expired", "RESET_TOKEN_INVALID"
        )

    new_password_hash = hash_password(body.new_password)
    await user_service.consume_password_reset_token(user.id, new_password_hash)
    logger.info("Password reset confirmed")
    return envelope(True, "Password has been reset", data=None)
