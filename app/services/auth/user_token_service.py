"""JWT issuance/verification for the `auth` module's PUBLIC USER session
cookie (ADR-020, approved).

Sibling to `app/services/auth/token_service.py` (admin), same structure,
but two of ADR-020's five isolation boundaries live here: a distinct
signing secret (`settings.user_jwt_secret`, never `admin_jwt_secret`) and a
distinct token `type` claim (`user_access`, never `admin_access`). This
file imports nothing from `token_service.py` and vice versa - zero shared
code path, so a token minted by one can never be decoded/accepted by the
other's `decode_*` function (wrong secret -> signature check fails outright;
even in the hypothetical case the secrets ever matched, the `type` claim
check below would still reject it).

`python-jose`, same as `token_service.py` - already a project dependency,
no reason to use a different JWT library for the second identity surface.
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from pydantic import BaseModel

from app.core.config import settings

logger = logging.getLogger(__name__)

# Distinguishes this token from an admin_access token (or any other JWT
# this codebase might ever issue) - require_user rejects a structurally-
# valid JWT that's missing this claim or has the wrong value, so a token
# minted for some other purpose (including an admin session token, even if
# it were somehow signed with this same secret) can never satisfy the user
# session gate.
_TOKEN_TYPE = "user_access"


class UserTokenPayload(BaseModel):
    sub: str  # users.email
    exp: datetime
    iat: datetime
    type: str


def create_user_access_token(email: str) -> str:
    """Mints a signed JWT for an authenticated public user.

    Never log the returned token - only its existence/expiry, never the
    value itself (same posture as create_admin_access_token).
    """
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=settings.user_jwt_expires_hours)
    payload = {"sub": email, "iat": now, "exp": expires_at, "type": _TOKEN_TYPE}
    return jwt.encode(payload, settings.user_jwt_secret, algorithm=settings.user_jwt_algorithm)


def decode_user_access_token(token: str) -> Optional[UserTokenPayload]:
    """Validates signature + expiry (python-jose's `jwt.decode` checks `exp`
    itself) and the `type` claim. Returns None on ANY failure - malformed,
    expired, wrong signature, wrong `type` - so callers (`require_user`)
    have exactly one failure branch to handle, all mapped to the same 401.

    Never logs the raw token value.
    """
    try:
        raw = jwt.decode(token, settings.user_jwt_secret, algorithms=[settings.user_jwt_algorithm])
    except JWTError as e:
        logger.warning("User JWT rejected: %s", str(e))
        return None
    if raw.get("type") != _TOKEN_TYPE:
        logger.warning("User JWT rejected: wrong token type")
        return None
    try:
        return UserTokenPayload(**raw)
    except Exception as e:
        logger.warning("User JWT rejected: malformed payload: %s", str(e))
        return None
