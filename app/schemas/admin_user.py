"""Pydantic models for the `admin_users` collection.

New collection for this feature (founder-approved architecture decisions,
ADR-019 pending) - flagged per CLAUDE.md's "don't invent a new collection
without flagging it" rule, same as `ai_tools_usage`/`seo_tools_usage`
before it. Backs a small, human-admin-only login gate for an upcoming
homepage-sections CMS; **not** the existing `api_keys` service-to-service
mechanism (`app/core/security.py::verify_api_key`), which is untouched.

No public signup ever creates a document here - the only writer is
`backend/scripts/seed_admin.py`. Never stores a plaintext password, only
`password_hash` (bcrypt via passlib - `app/services/auth/password_service.py`).

`failed_login_attempts`/`locked_until` implement the brute-force mitigation
on `POST /auth/login` as two extra fields on this same, already-approved
collection rather than introducing a second new collection just to hold a
per-account attempt counter - keeps this feature to exactly one new
collection instead of two, and (unlike an in-process-memory counter) stays
correct across multiple gunicorn worker processes on the VPS.

Collection: `admin_users` (lowercase-plural, matches this repo's other
collections). Fields: snake_case, matching this collection's own
`email`/`password_hash`/`created_at` spec (the camelCase convention in
Handbook Part C.9 describes `files`/`jobs`/etc.; `admin_users` was
specified with snake_case field names directly in the approved
architecture decisions for this feature, so that's followed here instead).
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PyObjectId


class AdminUserBase(BaseModel):
    # Always stored lowercased/stripped (app/services/auth/admin_user_service.py
    # normalizes this on both create and lookup) so login can't be bypassed
    # or double-registered via case/whitespace variants of the same address.
    email: str = Field(..., min_length=3, max_length=254)
    password_hash: str = Field(..., description="bcrypt hash only - never a plaintext password")
    failed_login_attempts: int = Field(default=0, ge=0)
    locked_until: Optional[datetime] = Field(
        default=None,
        description=(
            "Set when failed_login_attempts crosses settings.admin_login_max_attempts; "
            "login is refused until this time passes, then the counter resets."
        ),
    )


class AdminUserCreate(AdminUserBase):
    """Shape used when inserting a new `admin_users` document (seed script only)."""

    created_at: datetime = Field(default_factory=datetime.utcnow)


class AdminUserDocument(AdminUserBase):
    """Shape of an `admin_users` document as read back from MongoDB."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    created_at: datetime
