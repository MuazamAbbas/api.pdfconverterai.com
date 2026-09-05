"""`admin_users` collection CRUD for the `auth` module.

Owns every read/write against `db.admin_users`, mirroring
`app/services/jobs/service.py`'s "one module owns its collection" pattern
(Handbook Part C.3). No public signup route exists anywhere - the only
writer is `backend/scripts/seed_admin.py`. `app/routers/auth.py` (login)
only reads/updates the lockout fields on an existing document; it never
inserts one. `backend/scripts/reset_admin_password.py` is the only writer
for an out-of-band password reset (no `/auth/forgot-password` route exists
for admin accounts, by design - see that script's docstring).
"""
import logging
from datetime import datetime, timedelta
from typing import Optional

from pymongo.errors import DuplicateKeyError

from app.core.config import settings
from app.core.database import db
from app.schemas.admin_user import AdminUserCreate, AdminUserDocument

logger = logging.getLogger(__name__)


def _normalize_email(email: str) -> str:
    return email.strip().lower()


async def get_admin_user_by_email(email: str) -> Optional[AdminUserDocument]:
    doc = await db.admin_users.find_one({"email": _normalize_email(email)})
    return AdminUserDocument(**doc) if doc else None


async def create_admin_user(email: str, password_hash: str) -> AdminUserDocument:
    """Used only by `backend/scripts/seed_admin.py` - never by an HTTP route
    (no `/auth/register` or `/admin/signup` exists, by design)."""
    normalized = _normalize_email(email)
    existing = await db.admin_users.find_one({"email": normalized})
    if existing is not None:
        raise ValueError(f"An admin_users document already exists for {normalized}")
    admin_create = AdminUserCreate(email=normalized, password_hash=password_hash)
    try:
        insert_result = await db.admin_users.insert_one(admin_create.model_dump())
    except DuplicateKeyError:
        # The `admin_users_email_unique` index (app/core/database.py) is the
        # real guarantee against a race between two concurrent seed-script
        # runs; the find-before-insert check above is just a fast, friendly
        # first check for the common (non-concurrent) case.
        raise ValueError(f"An admin_users document already exists for {normalized}")
    doc = await db.admin_users.find_one({"_id": insert_result.inserted_id})
    logger.info("Created admin_users document id=%s", insert_result.inserted_id)
    return AdminUserDocument(**doc)


def is_locked(admin: AdminUserDocument) -> bool:
    return admin.locked_until is not None and admin.locked_until > datetime.utcnow()


async def register_failed_login(email: str) -> None:
    """Increments `failed_login_attempts`; sets `locked_until` once the
    configured threshold is reached. A no-op (not an error) if `email`
    doesn't match any document - callers must not use this to infer
    whether an email exists (Handbook Part C.10 / this feature's
    no-user-enumeration requirement)."""
    normalized = _normalize_email(email)
    admin = await db.admin_users.find_one({"email": normalized})
    if admin is None:
        return
    new_count = admin.get("failed_login_attempts", 0) + 1
    update: dict = {"failed_login_attempts": new_count}
    if new_count >= settings.admin_login_max_attempts:
        update["locked_until"] = datetime.utcnow() + timedelta(
            minutes=settings.admin_login_lockout_minutes
        )
        logger.warning(
            "Admin account locked after %d failed attempts (locked_until set)", new_count
        )
    await db.admin_users.update_one({"_id": admin["_id"]}, {"$set": update})


async def reset_failed_login(email: str) -> None:
    """Called on a successful login - clears the lockout state."""
    normalized = _normalize_email(email)
    await db.admin_users.update_one(
        {"email": normalized},
        {"$set": {"failed_login_attempts": 0, "locked_until": None}},
    )


async def reset_admin_password(
    email: str, password_hash: str, operator: Optional[str] = None
) -> AdminUserDocument:
    """Used only by `backend/scripts/reset_admin_password.py` - an operator-run,
    out-of-band recovery path (mirrors `create_admin_user`'s "script is the
    only writer" pattern). Also clears any lockout state, since a stale
    `locked_until` would otherwise silently defeat the new password.

    Must never be called from a network-reachable path: unlike
    `attempt_login`/`register_failed_login`, the "no such email" branch below
    raises immediately with no constant-time/dummy-hash guard, so it is a
    user-enumeration oracle if ever wired behind an HTTP route.

    `operator` (typically `getpass.getuser()` from the calling script) is
    logged alongside the account id so an app-log line ties this sensitive,
    unauthenticated-by-design action back to *who* ran it - the trust model
    here is "VPS shell access is the authentication," so this is a
    correlation aid for that shell/auth trail, not an access control."""
    normalized = _normalize_email(email)
    existing = await db.admin_users.find_one({"email": normalized})
    if existing is None:
        raise ValueError(f"No admin_users document exists for {normalized}")
    await db.admin_users.update_one(
        {"_id": existing["_id"]},
        {
            "$set": {
                "password_hash": password_hash,
                "failed_login_attempts": 0,
                "locked_until": None,
            }
        },
    )
    logger.info(
        "Reset password_hash and cleared lockout for admin_users id=%s (operator=%s)",
        existing["_id"],
        operator or "unknown",
    )
    doc = await db.admin_users.find_one({"_id": existing["_id"]})
    return AdminUserDocument(**doc)
