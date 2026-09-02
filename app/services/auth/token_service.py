"""JWT issuance/verification for the `auth` module's admin session cookie.

Deliberately separate from `app/core/security.py::verify_api_key` (opaque
`x-api-key` lookup against the `api_keys` collection) - this signs/verifies
a short-lived JWT instead, carried in an httpOnly cookie
(`app/routers/auth.py`), never a bearer header or localStorage (spec).

`python-jose` per the approved architecture decisions (both `python-jose`
and `pyjwt` are already in requirements.txt).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from jose import JWTError, jwt
from pydantic import BaseModel

from app.core.config import settings

logger = logging.getLogger(__name__)

# Distinguishes this token from any other JWT this codebase might ever
# issue in the future - `require_admin` rejects a structurally-valid JWT
# that's missing this claim or has the wrong value, so a token minted for
# some other purpose can never accidentally satisfy the admin gate.
_TOKEN_TYPE = "admin_access"


class AdminTokenPayload(BaseModel):
    sub: str  # admin_users.email
    exp: datetime
    iat: datetime
    type: str


def create_admin_access_token(email: str) -> str:
    """Mints a signed, short-lived JWT for an authenticated admin.

    Never log the returned token - only its existence/expiry, never the
    value itself (Handbook Part C.10 / this feature's spec).
    """
    now = datetime.utcnow()
    expires_at = now + timedelta(hours=settings.admin_jwt_expires_hours)
    payload = {"sub": email, "iat": now, "exp": expires_at, "type": _TOKEN_TYPE}
    return jwt.encode(payload, settings.admin_jwt_secret, algorithm=settings.admin_jwt_algorithm)


def decode_admin_access_token(token: str) -> Optional[AdminTokenPayload]:
    """Validates signature + expiry (python-jose's `jwt.decode` checks `exp`
    itself) and the `type` claim. Returns None on ANY failure - malformed,
    expired, wrong signature, wrong `type` - so callers (`require_admin`)
    have exactly one failure branch to handle, all mapped to the same 401.

    Never logs the raw token value.
    """
    try:
        raw = jwt.decode(token, settings.admin_jwt_secret, algorithms=[settings.admin_jwt_algorithm])
    except JWTError as e:
        logger.warning("Admin JWT rejected: %s", str(e))
        return None
    if raw.get("type") != _TOKEN_TYPE:
        logger.warning("Admin JWT rejected: wrong token type")
        return None
    try:
        return AdminTokenPayload(**raw)
    except Exception as e:
        logger.warning("Admin JWT rejected: malformed payload: %s", str(e))
        return None
