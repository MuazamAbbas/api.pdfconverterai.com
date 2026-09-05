"""`content_tool_metadata` collection CRUD for the `content` module
(ADR-021's foundation, Tools Metadata CMS feature spec approved 2026-09-04 -
see `docs/roadmap/SPRINT_STATUS.md`'s 2026-09-04 entry).

Owns every read/write against `db.content_tool_metadata`, mirroring
`categories_service.py`'s "one module owns its collection" pattern
(Handbook Part C.3). Called by `app/routers/content.py` - no HTTP concerns
here (no `HTTPException`), same convention as `categories_service.py`:
raises plain exception classes the router translates into
`app.shared.responses.api_error(...)`.

**`category` validation**: every create/update that touches `category`
resolves it against `categories_service.list_categories(ContentType.TOOL_METADATA)`
- the value must match one of those slugs or the write is rejected outright
with `InvalidCategory`. See `app/schemas/content_tool_metadata.py`'s module
docstring for why this is a service-layer, not schema-layer, check.

**`tags` normalization**: every create/update that touches `tags` runs each
raw string through `tags_service.get_or_create_tag`, replacing the payload's
`tags` list with the returned canonical slugs before the document is
written - never raw admin-typed strings land in `db.content_tool_metadata`.

**`slug` immutability**: `ContentToolMetadataUpdate` has no `slug` field at
all (enforced at the schema layer already, see that module's docstring) -
this service never accepts or writes a slug change on update.
"""
import logging
from datetime import datetime

from pymongo.errors import DuplicateKeyError

from app.core.database import db
from app.schemas.content_category import ContentType
from app.schemas.content_tool_metadata import (
    ContentToolMetadataCreate,
    ContentToolMetadataDocument,
    ContentToolMetadataUpdate,
)
from app.services.content.categories_service import list_categories
from app.services.content.tags_service import get_or_create_tag

logger = logging.getLogger(__name__)


class ToolMetadataNotFound(Exception):
    """Raised when a `slug` doesn't resolve to any `content_tool_metadata`
    document."""


class ToolMetadataSlugConflict(Exception):
    """Raised when a create would collide with an existing `slug` - the
    `content_tool_metadata_slug_unique` index (app/core/database.py) is the
    actual guarantee; this is a clean, client-safe translation of the
    resulting `DuplicateKeyError`."""


class InvalidCategory(Exception):
    """Raised when `category` does not match any existing
    `content_categories` slug with `content_type='tool_metadata'` - see
    this module's docstring."""


def derive_slug_from_href(href: str) -> str:
    """Returns the last path segment of a `tools-registry.ts`-style href,
    e.g. `/tools/pdf/pdf-merger` -> `pdf-merger`, `/tools/word-counter` ->
    `word-counter`. Documented derivation assumption from
    `app/schemas/content_tool_metadata.py`'s module docstring. Not called by
    the create route (which takes `slug` directly per
    `ContentToolMetadataCreate`'s schema) - exported for the frontend admin
    UI or a future seed/migration script to reuse rather than
    reimplementing this logic elsewhere.
    """
    return href.rstrip("/").rsplit("/", 1)[-1]


async def _validate_category(category: str) -> None:
    valid_categories = await list_categories(content_type=ContentType.TOOL_METADATA)
    valid_slugs = {c.slug for c in valid_categories}
    if category not in valid_slugs:
        logger.warning("Rejected content_tool_metadata write, unknown category %r", category)
        raise InvalidCategory(category)


async def _normalize_tags(tags: list[str]) -> list[str]:
    canonical: list[str] = []
    for raw_tag in tags:
        tag_doc = await get_or_create_tag(raw_tag)
        canonical.append(tag_doc.slug)
    # De-duplicate while preserving first-seen order - e.g. raw tags
    # ["SEO", "seo"] both correctly resolve to the same canonical `seo`
    # tags-collection document, but without this the content_tool_metadata
    # row itself would store ["seo", "seo"].
    return list(dict.fromkeys(canonical))


async def get_by_slug(slug: str) -> ContentToolMetadataDocument:
    """Backs the public `GET /v1/content/tool-metadata/{slug}` route."""
    doc = await db.content_tool_metadata.find_one({"slug": slug})
    if doc is None:
        raise ToolMetadataNotFound(slug)
    return ContentToolMetadataDocument(**doc)


async def list_all() -> list[ContentToolMetadataDocument]:
    """Every tool metadata row, sorted alphabetically by `slug` - backs a
    future admin list view (an admin can't edit a row they can't see in a
    list). Not explicitly required by AC1-5 as a public-facing route, but
    the admin write routes are meaningless without some way to enumerate
    existing rows first, so the admin `GET /v1/content/tool-metadata` route
    in `app/routers/content.py` calls this."""
    cursor = db.content_tool_metadata.find({}).sort("slug", 1)
    return [ContentToolMetadataDocument(**doc) async for doc in cursor]


async def create_tool_metadata(body: ContentToolMetadataCreate) -> ContentToolMetadataDocument:
    await _validate_category(body.category)

    now = datetime.utcnow()
    insert_doc = body.model_dump()
    insert_doc["tags"] = await _normalize_tags(body.tags)
    # Never trust caller-supplied created_at/updated_at (both are real fields
    # on ContentToolMetadataCreate, so extra="forbid" doesn't block a client
    # from setting them) - always stamp fresh server-side, mirroring
    # categories_service.create_category's identical override.
    insert_doc["created_at"] = now
    insert_doc["updated_at"] = now

    try:
        insert_result = await db.content_tool_metadata.insert_one(insert_doc)
    except DuplicateKeyError as exc:
        logger.warning("Rejected duplicate content_tool_metadata slug on create: %s", body.slug)
        raise ToolMetadataSlugConflict(body.slug) from exc

    doc = await db.content_tool_metadata.find_one({"_id": insert_result.inserted_id})
    logger.info("Created content_tool_metadata document id=%s slug=%s", insert_result.inserted_id, body.slug)
    return ContentToolMetadataDocument(**doc)


async def update_tool_metadata(slug: str, body: ContentToolMetadataUpdate) -> ContentToolMetadataDocument:
    existing = await get_by_slug(slug)  # raises ToolMetadataNotFound

    if body.category is not None:
        await _validate_category(body.category)

    update_doc: dict = {"updated_at": datetime.utcnow()}
    if body.title is not None:
        update_doc["title"] = body.title
    if body.category is not None:
        update_doc["category"] = body.category
    if body.icon is not None:
        update_doc["icon"] = body.icon
    if body.description is not None:
        update_doc["description"] = body.description
    if body.tags is not None:
        update_doc["tags"] = await _normalize_tags(body.tags)
    if body.how_to_use is not None:
        update_doc["how_to_use"] = body.how_to_use
    if body.faq is not None:
        update_doc["faq"] = body.faq
    if body.ad_slot is not None:
        update_doc["ad_slot"] = body.ad_slot.model_dump()

    await db.content_tool_metadata.update_one({"_id": existing.id}, {"$set": update_doc})

    doc = await db.content_tool_metadata.find_one({"_id": existing.id})
    logger.info("Updated content_tool_metadata document slug=%s", slug)
    return ContentToolMetadataDocument(**doc)


async def delete_tool_metadata(slug: str) -> None:
    existing = await get_by_slug(slug)  # raises ToolMetadataNotFound
    await db.content_tool_metadata.delete_one({"_id": existing.id})
    logger.info("Deleted content_tool_metadata document slug=%s", slug)
