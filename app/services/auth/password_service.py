"""Password hashing/verification for the `auth` module - the ONLY place in
this codebase allowed to touch a plaintext password (founder-approved
architecture decisions for this feature). Nothing else - not `admin`, not
any router - should import `passlib` directly.

bcrypt via passlib's `CryptContext`, per spec. Deliberately separate from
`app/core/security.py::verify_api_key`, which authenticates a completely
different kind of credential (an opaque service-to-service API key, not a
human password) and must not be touched by this work.
"""
import logging

from passlib.context import CryptContext

logger = logging.getLogger(__name__)

_pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")

# A precomputed hash of a value nobody will ever actually set as a real
# password, used only to burn a constant amount of bcrypt time when an
# email lookup misses (see app/services/auth/login_service.py). Without
# this, "unknown email" would return faster than "known email, wrong
# password", which is itself a (weaker, timing-based) user-enumeration
# signal even though the HTTP response body is identical in both cases.
_DUMMY_HASH = _pwd_context.hash("not-a-real-password-used-only-for-timing-parity")


def hash_password(plain_password: str) -> str:
    """Never log `plain_password` - not even at DEBUG level."""
    return _pwd_context.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """Never log either argument."""
    try:
        return _pwd_context.verify(plain_password, password_hash)
    except (ValueError, TypeError) as e:
        # Malformed/foreign hash format - fail closed, never raise a stack
        # trace up to the caller (Handbook Part C.10).
        logger.error("Password verification failed on malformed hash: %s", str(e))
        return False


def verify_password_constant_time_dummy(plain_password: str) -> None:
    """Burns roughly the same bcrypt time as a real `verify_password` call,
    for the "email not found" branch of login - see `_DUMMY_HASH` above.
    Return value is intentionally discarded by the caller; this always
    "fails" regardless of `plain_password`.
    """
    try:
        _pwd_context.verify(plain_password, _DUMMY_HASH)
    except (ValueError, TypeError):
        pass
