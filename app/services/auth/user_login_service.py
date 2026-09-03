"""Login orchestration for `POST /auth/users/login` (the `auth` module,
public-user identity surface - ADR-020, approved).

Sibling to `app/services/auth/login_service.py` (admin) - same shape
(`attempt_*` returns an outcome enum rather than raising per-case
exceptions, so the router maps DENIED/LOCKED to the identical 401 body -
this feature's own no-user-enumeration requirement, same as admin login),
but deliberately its own file/enum rather than importing `login_service`'s
`LoginResult`/`attempt_login`: keeps this identity surface's orchestration
independently readable/testable and avoids any accidental coupling between
the two login code paths, in the same spirit as ADR-020's isolation table
(even though "login orchestration" itself isn't one of the five named
boundaries - the boundaries are collection/cookie/secret/claim/guard).
"""
import logging
from enum import Enum
from typing import Optional

from app.services.auth import user_service
from app.services.auth.password_service import (
    verify_password,
    verify_password_constant_time_dummy,
)
from app.services.auth.user_token_service import create_user_access_token

logger = logging.getLogger(__name__)


class UserLoginResult(str, Enum):
    ALLOWED = "allowed"
    DENIED = "denied"  # unknown email OR wrong password - same external signal
    LOCKED = "locked"  # account temporarily locked out (brute-force mitigation)


async def attempt_user_login(email: str, password: str) -> tuple[UserLoginResult, Optional[str]]:
    """Returns (result, token). `token` is only non-None when result is
    ALLOWED. Never returns/logs the plaintext `password`."""
    user = await user_service.get_user_by_email(email)

    if user is None:
        # Burn comparable bcrypt time to a real verify, so "no such
        # account" isn't distinguishable from "wrong password" via timing.
        verify_password_constant_time_dummy(password)
        logger.info("Login attempt for unknown user email")
        return UserLoginResult.DENIED, None

    if user_service.is_locked(user):
        # Same timing-parity reasoning as login_service.attempt_login's
        # LOCKED branch - burn the same bcrypt time as the other two
        # rejection paths so a locked account isn't distinguishable via
        # response timing.
        verify_password_constant_time_dummy(password)
        logger.warning("Login attempt for locked user account")
        return UserLoginResult.LOCKED, None

    if not verify_password(password, user.password_hash):
        await user_service.register_failed_login(email)
        logger.info("Login attempt failed (bad password)")
        return UserLoginResult.DENIED, None

    await user_service.reset_failed_login(email)
    token = create_user_access_token(user.email)
    logger.info("User login succeeded")
    return UserLoginResult.ALLOWED, token
