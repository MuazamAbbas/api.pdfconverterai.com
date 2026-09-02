"""Login orchestration for `POST /auth/login` (the `auth` module).

Kept out of `app/routers/auth.py` itself so the route handler stays thin
(Handbook Part C.3: business logic belongs in the module's service layer,
not the route handler) and so this logic is unit-testable without needing
a running ASGI app.

The single entry point, `attempt_login`, deliberately returns one of three
outcomes (`ALLOWED`, `DENIED`, `LOCKED`) rather than raising per-case
exceptions with different messages - the router maps `DENIED` and `LOCKED`
to the exact same 401 body/status (this feature's explicit
no-user-enumeration requirement: wrong email and wrong password must be
indistinguishable to the caller). `LOCKED` is kept as a distinct enum value
only so the router can log a more specific line server-side; it must not
change the HTTP response shape.
"""
import logging
from enum import Enum
from typing import Optional

from app.services.auth import admin_user_service
from app.services.auth.password_service import (
    verify_password,
    verify_password_constant_time_dummy,
)
from app.services.auth.token_service import create_admin_access_token

logger = logging.getLogger(__name__)


class LoginResult(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"  # unknown email OR wrong password - same external signal
    LOCKED = "locked"  # account temporarily locked out (brute-force mitigation)


async def attempt_login(email: str, password: str) -> tuple[LoginResult, Optional[str]]:
    """Returns (result, token). `token` is only non-None when result is
    ALLOWED. Never returns/logs the plaintext `password`."""
    admin = await admin_user_service.get_admin_user_by_email(email)

    if admin is None:
        # Burn comparable bcrypt time to a real verify, so "no such
        # account" isn't distinguishable from "wrong password" via timing.
        verify_password_constant_time_dummy(password)
        logger.info("Login attempt for unknown admin email")
        return LoginResult.DENIED, None

    if admin_user_service.is_locked(admin):
        # Burn the same bcrypt time as the unknown-email/wrong-password
        # branches above/below - `is_locked()` itself is a cheap datetime
        # comparison, so without this a "locked" account would respond
        # measurably faster than the other two rejection paths, letting an
        # attacker distinguish "this account exists and is currently
        # locked" via response timing (security-reviewer finding).
        verify_password_constant_time_dummy(password)
        logger.warning("Login attempt for locked admin account")
        return LoginResult.LOCKED, None

    if not verify_password(password, admin.password_hash):
        await admin_user_service.register_failed_login(email)
        logger.info("Login attempt failed (bad password)")
        return LoginResult.DENIED, None

    await admin_user_service.reset_failed_login(email)
    token = create_admin_access_token(admin.email)
    logger.info("Admin login succeeded")
    return LoginResult.ALLOWED, token
