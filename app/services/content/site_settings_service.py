"""`site_settings` singleton document read/write for the `content` module
(ADR-021's module boundary) - Admin-managed SEO & site-verification settings,
Round 1 (ads.txt + verification codes). Founder-approved spec, see
`docs/roadmap/SPRINT_STATUS.md`'s 2026-09-19 "Spec approved: Admin-managed
SEO & site-verification settings (Round 1: ads.txt + verification codes)"
entry.

Owns every read/write against `db.site_settings`, mirroring
`app/services/content/categories_service.py`'s "one module owns its
collection" pattern (Handbook Part C.3). Called by `app/routers/content.py`
- no HTTP concerns here (no `HTTPException`/`api_error`), same convention as
every other service module in this package.

See `app/schemas/site_settings.py`'s module docstring for the full singleton
design rationale (`SITE_SETTINGS_SINGLETON_ID`, seedless-by-design, no
secondary index). This module just implements the two read/write patterns
that docstring already specifies:

- `get_site_settings()` - a single point `find_one` by the fixed `_id`,
  returning `default_site_settings()` (all-empty defaults) when no document
  exists yet. Never inserts anything - a `GET` must stay side-effect-free.
- `update_site_settings(body)` - a single atomic `update_one(..., upsert=True)`
  so the very first admin write both creates and updates the one document.
"""
import logging
from datetime import datetime

from app.core.database import db
from app.schemas.site_settings import (
    SITE_SETTINGS_SINGLETON_ID,
    SiteSettingsRead,
    SiteSettingsUpdate,
    default_site_settings,
)

logger = logging.getLogger(__name__)


def _to_read(doc: dict) -> SiteSettingsRead:
    """`SiteSettingsRead` only declares the two Round 1 fields and inherits
    `extra="forbid"` from `SiteSettingsBase`, but a real `site_settings`
    document always also carries `_id`/`created_at`/`updated_at` - unpacking
    the raw doc directly (`SiteSettingsRead(**doc)`) raises a validation
    error on every document that actually exists (test-runner finding: every
    GET after any write, and the PUT's own re-read, 500'd). Pick only the
    declared fields instead of unpacking the whole document."""
    return SiteSettingsRead(
        ads_txt_content=doc["ads_txt_content"],
        verification_codes=doc["verification_codes"],
    )


async def get_site_settings() -> SiteSettingsRead:
    """Backs the public `GET /v1/content/site-settings` route. Returns the
    sane all-empty defaults (never a 404, never an insert) when no document
    has ever been written yet - see `app/schemas/site_settings.py`'s
    "Seedless by design" docstring section."""
    doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    if doc is None:
        logger.debug("No site_settings document exists yet, returning defaults")
        return default_site_settings()
    return _to_read(doc)


async def update_site_settings(body: SiteSettingsUpdate) -> SiteSettingsRead:
    """Backs the admin `PUT /v1/content/site-settings` route. Full-replace
    upsert of the one `site_settings` document by its fixed `_id` - see
    `app/schemas/site_settings.py`'s "Singleton mechanism" docstring section."""
    now = datetime.utcnow()
    await db.site_settings.update_one(
        {"_id": SITE_SETTINGS_SINGLETON_ID},
        {
            "$set": {
                "ads_txt_content": body.ads_txt_content,
                "verification_codes": [vc.model_dump() for vc in body.verification_codes],
                "updated_at": now,
            },
            "$setOnInsert": {"created_at": now},
        },
        upsert=True,
    )
    logger.info("Upserted site_settings document (verification_codes count=%d)", len(body.verification_codes))
    doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    return _to_read(doc)
