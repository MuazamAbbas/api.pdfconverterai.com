"""`tags` collection CRUD for the new `content` module (ADR-021).

**The only write path into `db.tags` is `get_or_create_tag` below.** Per
`app/schemas/tag.py`'s module docstring (ADR-021's "normalize-and-upsert"
decision), there is no direct create/update/delete API for tags and there
never should be - a future backend-builder adding a direct CRUD route for
this collection would defeat the entire point of normalize-and-upsert
(letting "SEO"/"seo"/"Seo" all resolve to one canonical document without
requiring pre-curation). If either CMS needs a read-only
list/autocomplete route, it should call `list_tags` below, not add a new
write path.

Mirrors `app/services/admin/homepage_sections_service.py` / `app/services/jobs/service.py`'s
"one module owns its collection" pattern (Handbook Part C.3). No HTTP
concerns here - raises plain exceptions only where relevant (none needed
today; `get_or_create_tag` cannot meaningfully fail on valid input beyond
normalizing to an empty string, which is rejected with a plain `ValueError`
since there's no sensible tag to create).
"""
import logging
import re
from datetime import datetime

from pymongo.errors import DuplicateKeyError

from app.core.database import db
from app.schemas.tag import TagDocument

logger = logging.getLogger(__name__)

_SLUG_COLLAPSE_RE = re.compile(r"[^a-z0-9]+")


def normalize_tag_slug(raw_tag: str) -> str:
    """Trim, lowercase, and slugify a raw tag string: runs of anything
    that isn't `[a-z0-9]` collapse to a single hyphen, and leading/trailing
    hyphens are stripped - e.g. " SEO Tips! " -> "seo-tips". Shared by
    `get_or_create_tag` and available standalone so callers (or tests) can
    check whether two raw strings would normalize to the same tag without
    hitting the database."""
    lowered = raw_tag.strip().lower()
    slug = _SLUG_COLLAPSE_RE.sub("-", lowered).strip("-")
    return slug


async def get_or_create_tag(raw_tag: str) -> TagDocument:
    """Normalizes `raw_tag` and upserts the canonical `tags` document,
    returning it. The only writer of `db.tags` - see this module's
    docstring.

    `label` keeps the *first-seen* display casing (e.g. the first caller to
    submit "SEO" wins the display label even if a later caller submits
    "seo") - subsequent calls for an already-existing slug never overwrite
    `label`, they just return the existing document unchanged.

    The find-before-upsert check below is a TOCTOU-vulnerable first line of
    defense only, same caveat `admin_user_service.create_admin_user`'s
    docstring gives for its own find-before-insert check - the
    `tags_slug_unique` index (app/core/database.py) is what actually
    prevents two concurrent callers from creating duplicate documents for
    the same normalized slug.
    """
    slug = normalize_tag_slug(raw_tag)
    if not slug:
        raise ValueError(f"raw_tag {raw_tag!r} normalizes to an empty slug")

    existing = await db.tags.find_one({"slug": slug})
    if existing is not None:
        return TagDocument(**existing)

    insert_doc = {
        "slug": slug,
        "label": raw_tag.strip(),
        "created_at": datetime.utcnow(),
    }
    try:
        insert_result = await db.tags.insert_one(insert_doc)
    except DuplicateKeyError:
        # Lost the race to a concurrent caller normalizing the same slug -
        # the index is the real guarantee (see docstring); just read back
        # whatever the winner inserted.
        winner = await db.tags.find_one({"slug": slug})
        return TagDocument(**winner)

    doc = await db.tags.find_one({"_id": insert_result.inserted_id})
    logger.info("Created tags document id=%s slug=%s", insert_result.inserted_id, slug)
    return TagDocument(**doc)


async def list_tags() -> list[TagDocument]:
    """Every tag, sorted alphabetically by `slug` - backs a future
    read-only `GET /v1/content/tags` autocomplete route. Not part of this
    task's router scope, but the service function is provided so
    backend-builder doesn't need to add a second read path into `db.tags`."""
    cursor = db.tags.find({}).sort("slug", 1)
    return [TagDocument(**doc) async for doc in cursor]
