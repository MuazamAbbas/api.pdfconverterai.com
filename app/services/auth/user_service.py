"""`users` collection CRUD for the `auth` module (ADR-020, approved).

Mirrors `app/services/auth/admin_user_service.py`'s shape closely (find-
before-insert + unique-index-backed create, `_normalize_email`, lockout
increment/reset) - deliberately structurally isolated from it per ADR-020's
five-boundary isolation table: this file only ever touches `db.users`,
never `db.admin_users`, and nothing in `admin_user_service.py` touches
`db.users`. Unlike `admin_user_service.create_admin_user` (seed-script-only,
no public writer), `create_user` here IS the public signup writer -
`POST /auth/signup` (`app/routers/auth.py`) is the only caller.

Also owns password-reset token issuance/consumption (`app/schemas/user.py`'s
`password_reset_token_hash`/`password_reset_expires_at` fields). The raw,
emailed token is high-entropy random data (`secrets.token_urlsafe(32)`,
256 bits), not a low-entropy human password, so it's hashed with SHA-256
here rather than routed through `password_service.py`'s bcrypt
`hash_password`/`verify_password`: bcrypt's per-call random salt means two
hashes of the identical input never match, which would make a direct
`find_one({"password_reset_token_hash": ...})` lookup by hash impossible -
exactly what `get_user_by_valid_reset_token` needs, since the confirm
endpoint receives only the raw token (from a reset-link query param), never
the owning email. SHA-256 is appropriate specifically because the input
being hashed is already uniformly random and high-entropy (unlike a human
password, where SHA-256 alone would be unsalted-and-fast-hash-weak against
guessing) - never store the raw token itself either way.
"""
import logging
import secrets
from datetime import datetime, timedelta
from hashlib import sha256
from typing import Optional

from bson import ObjectId
from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import db
from app.schemas.user import UserCreate, UserDocument

logger = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


def _hash_reset_token(raw_token: str) -> str:
    """Deterministic (unlike bcrypt) so a token can be looked up by its
    hash - see module docstring for why that's the right trade-off here."""
    return sha256(raw_token.encode("utf-8")).hexdigest()


async def get_user_by_email(email: str) -> Optional[UserDocument]:
    doc = await db.users.find_one({"email": _normalize_email(email)})
    return UserDocument(**doc) if doc else None


async def create_user(email: str, password_hash: str) -> UserDocument:
    """Used only by `POST /auth/signup` (`app/routers/auth.py`)."""
    normalized = _normalize_email(email)
    existing = await db.users.find_one({"email": normalized})
    if existing is not None:
        raise ValueError(f"A users document already exists for {normalized}")
    user_create = UserCreate(email=normalized, password_hash=password_hash)
    try:
        insert_result = await db.users.insert_one(user_create.model_dump())
    except DuplicateKeyError:
        # The `users_email_unique` index (app/core/database.py) is the real
        # guarantee against a race between two concurrent signup requests
        # for the same address; the find-before-insert check above is just
        # a fast, friendly first check for the common (non-concurrent) case
        # - same pattern as admin_user_service.create_admin_user.
        raise ValueError(f"A users document already exists for {normalized}")
    doc = await db.users.find_one({"_id": insert_result.inserted_id})
    logger.info("Created users document id=%s", insert_result.inserted_id)
    return UserDocument(**doc)


def is_locked(user: UserDocument) -> bool:
    return user.locked_until is not None and user.locked_until > datetime.utcnow()


async def register_failed_login(email: str) -> None:
    """Increments `failed_login_attempts`; sets `locked_until` once the
    configured threshold is reached. A no-op (not an error) if `email`
    doesn't match any document - callers must not use this to infer
    whether an email exists (this feature's no-user-enumeration
    requirement, same as admin_user_service.register_failed_login)."""
    normalized = _normalize_email(email)
    user = await db.users.find_one({"email": normalized})
    if user is None:
        return
    new_count = user.get("failed_login_attempts", 0) + 1
    update: dict = {"failed_login_attempts": new_count}
    if new_count >= settings.user_login_max_attempts:
        update["locked_until"] = datetime.utcnow() + timedelta(
            minutes=settings.user_login_lockout_minutes
        )
        logger.warning(
            "User account locked after %d failed attempts (locked_until set)", new_count
        )
    await db.users.update_one({"_id": user["_id"]}, {"$set": update})


async def reset_failed_login(email: str) -> None:
    """Called on a successful login - clears the lockout state."""
    normalized = _normalize_email(email)
    await db.users.update_one(
        {"email": normalized},
        {"$set": {"failed_login_attempts": 0, "locked_until": None}},
    )


async def issue_password_reset_token(email: str) -> Optional[str]:
    """Mints a new one-time reset token for `email`, overwriting (invalidating)
    any previously-issued token for this account (schema's documented
    single-active-token design). Returns the RAW token (only this function
    ever sees it in plaintext) for the caller to hand to the Tier 2 email
    job - never logged, never returned to the HTTP caller directly.

    Returns None if no account matches `email` - callers (the
    POST /auth/users/password-reset/request route) must not let this
    difference leak into the HTTP response (always 200 either way, per
    ADR-020's no-user-enumeration requirement).
    """
    normalized = _normalize_email(email)
    user = await db.users.find_one({"email": normalized})
    if user is None:
        return None

    raw_token = secrets.token_urlsafe(32)
    token_hash = _hash_reset_token(raw_token)
    expires_at = datetime.utcnow() + timedelta(
        minutes=settings.user_password_reset_token_expires_minutes
    )
    await db.users.update_one(
        {"_id": user["_id"]},
        {
            "$set": {
                "password_reset_token_hash": token_hash,
                "password_reset_expires_at": expires_at,
            }
        },
    )
    logger.info("Issued password reset token for user id=%s", user["_id"])
    return raw_token


async def get_user_by_valid_reset_token(raw_token: str) -> Optional[UserDocument]:
    """Looks up the user matching `raw_token`'s hash, but only if a token
    hash is actually set AND `password_reset_expires_at` hasn't passed yet -
    the application-layer expiry check `app/schemas/user.py`'s docstring
    calls for (deliberately NOT a Mongo TTL index - see that docstring).
    Returns None for "no such token" and "token expired" alike, so callers
    can't distinguish the two (same posture as login's DENIED/LOCKED
    collapsing to one HTTP response).
    """
    token_hash = _hash_reset_token(raw_token)
    doc = await db.users.find_one({"password_reset_token_hash": token_hash})
    if doc is None:
        return None
    user = UserDocument(**doc)
    if user.password_reset_expires_at is None or user.password_reset_expires_at < datetime.utcnow():
        return None
    return user


async def consume_password_reset_token(user_id: ObjectId, new_password_hash: str) -> None:
    """Sets the new password hash and clears both reset-token fields
    (single-use - the token can never be replayed after this). Also clears
    any lockout state: successfully proving possession of the one-time
    reset token is at least as strong a proof of ownership as a correct
    password, so there's no reason to keep an account locked out after
    this succeeds.
    """
    await db.users.update_one(
        {"_id": user_id},
        {
            "$set": {
                "password_hash": new_password_hash,
                "password_reset_token_hash": None,
                "password_reset_expires_at": None,
                "failed_login_attempts": 0,
                "locked_until": None,
            }
        },
    )
    logger.info("Password reset completed for user id=%s", user_id)
