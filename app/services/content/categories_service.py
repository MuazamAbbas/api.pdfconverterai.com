"""`content_categories` collection CRUD for the new `content` module
(ADR-021).

Owns every read/write against `db.content_categories`, mirroring
`app/services/admin/homepage_sections_service.py` / `app/services/jobs/service.py`'s
"one module owns its collection" pattern (Handbook Part C.3). Called by a
future `app/routers/content.py` (backend-builder's next step, not part of
this task) - no HTTP concerns here (no `HTTPException`), same convention as
`homepage_sections_service.py`: raises plain exception classes the router
translates into `app.shared.responses.api_error(...)`.

**The `content_type='tool_metadata'` read-only invariant** (ADR-021) is
enforced here, at the application layer, on every mutating function
(`create_category`, `update_category`, `delete_category`) - never a silent
no-op, always the dedicated `CategoryReadOnly` exception (the router is
expected to translate this to a `CATEGORY_READ_ONLY` error code). See
`app/schemas/content_category.py`'s module docstring for why there is no
DB-layer backstop for this specific constraint (unlike the unique `slug`
index, which does backstop duplicate-creation).

`create_category` also rejects `content_type='tool_metadata'` outright -
ADR-021's decision text only names "edit/delete" explicitly, but allowing a
second, API-created `tool_metadata` row would undermine the same invariant
(an admin could create a fake "pdf" category-shaped row causing exactly the
kind of tools-registry.ts/Mongo drift ADR-021 exists to prevent). The seed
script (`backend/scripts/seed_content_categories.py`) is therefore the only
writer of `tool_metadata` rows, and it bypasses this service layer
entirely - it writes to `db.content_categories` directly, the same pattern
`seed_homepage_sections.py` uses for its structural `tool_grid` document.
"""
import logging
from datetime import datetime
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from app.core.database import db
from app.schemas.content_category import (
    ContentCategoryCreate,
    ContentCategoryDocument,
    ContentCategoryReorderItem,
    ContentCategoryUpdate,
    ContentType,
)

logger = logging.getLogger(__name__)


class CategoryNotFound(Exception):
    """Raised when a category id doesn't resolve to any `content_categories`
    document (including a structurally-invalid ObjectId string)."""


class CategoryReadOnly(Exception):
    """Raised on any create/edit/delete attempt against a
    `content_type='tool_metadata'` row - see this module's docstring. The
    router is expected to translate this into a `CATEGORY_READ_ONLY`
    error code, never a silent no-op or a bare 500."""


class CategorySlugConflict(Exception):
    """Raised when a create/rename would collide with an existing `slug` -
    the `content_categories_slug_unique` index (app/core/database.py) is
    the actual guarantee; this is a clean, client-safe translation of the
    resulting `DuplicateKeyError`."""


def _to_object_id(category_id: str) -> Optional[ObjectId]:
    try:
        return ObjectId(category_id)
    except (InvalidId, TypeError):
        return None


async def list_categories(content_type: Optional[ContentType] = None) -> list[ContentCategoryDocument]:
    """All categories, optionally filtered by `content_type`, sorted by
    `order` - backs the public `GET /v1/content/categories` route (and its
    `?content_type=` filter)."""
    query = {"content_type": content_type.value} if content_type is not None else {}
    cursor = db.content_categories.find(query).sort("order", 1)
    return [ContentCategoryDocument(**doc) async for doc in cursor]


async def get_category(category_id: str) -> ContentCategoryDocument:
    oid = _to_object_id(category_id)
    if oid is None:
        raise CategoryNotFound(category_id)
    doc = await db.content_categories.find_one({"_id": oid})
    if doc is None:
        raise CategoryNotFound(category_id)
    return ContentCategoryDocument(**doc)


async def create_category(payload: ContentCategoryCreate) -> ContentCategoryDocument:
    """Admin-authored "create blog category" path only - see this module's
    docstring for why `content_type='tool_metadata'` is rejected here even
    though ADR-021's decision text only names edit/delete explicitly."""
    if payload.content_type == ContentType.TOOL_METADATA:
        logger.warning("Rejected create attempt for a content_type='tool_metadata' category via the API")
        raise CategoryReadOnly("tool_metadata categories may only be created by the seed script")

    now = datetime.utcnow()
    insert_doc = payload.model_dump()
    insert_doc["created_at"] = now
    insert_doc["updated_at"] = now
    try:
        insert_result = await db.content_categories.insert_one(insert_doc)
    except DuplicateKeyError as exc:
        logger.warning("Rejected duplicate content_categories slug on create: %s", exc)
        raise CategorySlugConflict(payload.slug) from exc
    doc = await db.content_categories.find_one({"_id": insert_result.inserted_id})
    logger.info("Created content_categories document id=%s slug=%s", insert_result.inserted_id, payload.slug)
    return ContentCategoryDocument(**doc)


async def update_category(category_id: str, update: ContentCategoryUpdate) -> ContentCategoryDocument:
    """Rejects outright with `CategoryReadOnly` if the existing document's
    `content_type` is `tool_metadata` - checked before anything else, and
    before any field-level merge, so a read-only row can never be partially
    written to."""
    existing = await get_category(category_id)  # raises CategoryNotFound
    if existing.content_type == ContentType.TOOL_METADATA:
        logger.warning("Rejected update attempt on read-only tool_metadata category %s", category_id)
        raise CategoryReadOnly(category_id)

    update_doc: dict = {"updated_at": datetime.utcnow()}
    if update.label is not None:
        update_doc["label"] = update.label
    if update.slug is not None:
        update_doc["slug"] = update.slug
    if update.order is not None:
        update_doc["order"] = update.order

    try:
        await db.content_categories.update_one({"_id": existing.id}, {"$set": update_doc})
    except DuplicateKeyError as exc:
        logger.warning("Rejected duplicate content_categories slug on update: %s", exc)
        raise CategorySlugConflict(update.slug) from exc

    doc = await db.content_categories.find_one({"_id": existing.id})
    logger.info("Updated content_categories document id=%s", category_id)
    return ContentCategoryDocument(**doc)


async def delete_category(category_id: str) -> None:
    existing = await get_category(category_id)  # raises CategoryNotFound
    if existing.content_type == ContentType.TOOL_METADATA:
        logger.warning("Rejected delete attempt on read-only tool_metadata category %s", category_id)
        raise CategoryReadOnly(category_id)
    await db.content_categories.delete_one({"_id": existing.id})
    logger.info("Deleted content_categories document id=%s", category_id)


async def reorder_categories(items: list[ContentCategoryReorderItem]) -> list[ContentCategoryDocument]:
    """Renumbers every category referenced in `items` to a consistent
    `0..N-1` range, sorted by the caller's requested `order` (ties broken by
    request-array position) - same pattern/reasoning as
    `homepage_sections_service.reorder_sections`. Rejects the whole batch
    with `CategoryReadOnly` if any referenced id is a `tool_metadata` row -
    tool categories' `order` is fixed by the seed script, never
    admin-reorderable."""
    object_ids: list[ObjectId] = []
    for item in items:
        oid = _to_object_id(item.id)
        if oid is None:
            raise CategoryNotFound(item.id)
        object_ids.append(oid)

    existing_docs = [doc async for doc in db.content_categories.find({"_id": {"$in": object_ids}})]
    if len(existing_docs) != len(object_ids):
        raise CategoryNotFound("one or more category ids in the reorder request do not exist")
    if any(doc["content_type"] == ContentType.TOOL_METADATA.value for doc in existing_docs):
        logger.warning("Rejected reorder attempt including one or more read-only tool_metadata categories")
        raise CategoryReadOnly("tool_metadata categories cannot be reordered")

    ranked = sorted(range(len(items)), key=lambda idx: (items[idx].order, idx))
    new_order_by_id = {object_ids[idx]: position for position, idx in enumerate(ranked)}

    operations = [
        UpdateOne({"_id": oid}, {"$set": {"order": new_order, "updated_at": datetime.utcnow()}})
        for oid, new_order in new_order_by_id.items()
    ]
    if operations:
        await db.content_categories.bulk_write(operations, ordered=False)
    logger.info("Reordered %d content_categories documents", len(operations))

    return await list_categories(ContentType.BLOG)
