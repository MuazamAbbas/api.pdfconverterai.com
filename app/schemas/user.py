"""Pydantic models for the `users` collection.

New collection for the Public User Auth feature (ADR-020, approved).
Structurally isolated from `admin_users` (ADR-019) per ADR-020's five-boundary
isolation table - this collection, its documents, and its indexes are the
"Collection" row of that table. The other four boundaries (cookie name,
JWT secret, token `type` claim, guard dependency) are enforced by
`user_token_service.py` / `app/core/user_auth.py`, not here - this module
owns schema + persistence only, per ADR-020's Option A scope
("A dedicated `users` *module* ... is deliberately not created yet").

Round 1 scope only (signup/login/session/password-reset) - no profile/
dashboard fields. `verified_at` exists for forward-compat with the future
email-verification ticket (ADR-020 Trade-offs: Round 1 never sets it).

Password hashing is `app/services/auth/password_service.py` (bcrypt via
passlib) - the same shared, already-generic service `admin_users` uses.
That file is untouched by this change; nothing here duplicates hashing
logic.

Password-reset token storage: a hashed one-time token + expiry stored
directly on this document (`password_reset_token_hash`,
`password_reset_expires_at`) rather than a separate
`password_reset_tokens` collection - see this module's docstring note
below and the index comment in `app/core/database.py::ensure_indexes`
for the full reasoning, including why this pair deliberately does NOT
get a TTL index.

Collection: `users` (lowercase-plural). Fields: snake_case, not this
repo's general camelCase convention (Handbook Part C.9) - same deviation
`admin_users` already established and documented in
`app/schemas/admin_user.py`: ADR-020 specifies these fields
(`email`, `password_hash`, `verified_at`, `created_at`,
`failed_login_attempts`, `locked_until`) directly in snake_case in the
approved architecture decision, so that's followed here for consistency
with the sibling identity collection rather than mixing conventions
within the same `auth` module. No `updated_at` field, for the same
reason `admin_users` has none: not specified by the owning ADR, and
every mutation path here (lockout fields, reset-token fields,
`verified_at`) already carries its own meaningful timestamp.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PyObjectId


class UserBase(BaseModel):
    # Always stored lowercased/stripped (app/services/auth/user_service.py,
    # backend-builder's next task, normalizes this on both create and
    # lookup - same convention as admin_user_service.py::_normalize_email)
    # so signup/login can't be bypassed or double-registered via
    # case/whitespace variants of the same address.
    email: str = Field(..., min_length=3, max_length=254)
    password_hash: str = Field(..., description="bcrypt hash only - never a plaintext password")
    # Round 1 never sets this (ADR-020 Trade-offs: no email verification
    # yet) - field exists so the future verification ticket doesn't need
    # a migration to add it.
    verified_at: Optional[datetime] = Field(default=None)
    failed_login_attempts: int = Field(default=0, ge=0)
    locked_until: Optional[datetime] = Field(
        default=None,
        description=(
            "Set when failed_login_attempts crosses the configured user-login "
            "lockout threshold; login is refused until this time passes, then "
            "the counter resets. Same pattern as admin_users.locked_until."
        ),
    )
    # Password-reset support (POST /auth/password-reset/request + /confirm).
    # Single active token at a time: a new reset request overwrites both
    # fields, invalidating any previously-issued token for this account.
    # `password_reset_token_hash` stores a hash of the one-time token sent
    # in the reset-email link, never the raw token itself (same
    # never-store-the-secret posture as password_hash) - the specific
    # hashing scheme is user_token_service.py's decision (backend-builder's
    # next task), not this schema's.
    password_reset_token_hash: Optional[str] = Field(default=None)
    password_reset_expires_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Reset link is rejected once this passes. Checked at the "
            "application layer (user_service.py), NOT enforced via a Mongo "
            "TTL index - see app/core/database.py::ensure_indexes for why a "
            "TTL index on this field would be actively dangerous here (it "
            "would delete the whole user document, not just the stale "
            "token, once the token expired)."
        ),
    )


class UserCreate(UserBase):
    """Shape used when inserting a new `users` document (signup)."""

    created_at: datetime = Field(default_factory=datetime.utcnow)


class UserDocument(UserBase):
    """Shape of a `users` document as read back from MongoDB."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    created_at: datetime
