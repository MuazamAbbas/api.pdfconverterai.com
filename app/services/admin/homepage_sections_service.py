"""`homepage_sections` collection CRUD for the `admin` module (ADR-019).

Owns every read/write against `db.homepage_sections`, mirroring
`app/services/jobs/service.py` / `app/services/auth/admin_user_service.py`'s
"one module owns its collection" pattern (Handbook Part C.3). Called by
`app/routers/admin.py` only - no HTTP concerns here (no `HTTPException`),
same convention as `app/services/files/service.py`: raises plain
`ValueError`/`LookupError`-style exceptions that the router translates into
`app.shared.responses.api_error(...)`.

Per `app/schemas/homepage_section.py`'s module docstring (database-agent's
notes, carried into this task's spec):
  - On update, any `content` change is merged onto the *existing* document's
    `type` and re-validated through `HomepageSectionBase` - the per-type
    shape/`tool_grid`-emptiness check applies to edits too, not just
    creates. Never trust `HomepageSectionUpdate.content` as already-valid.
  - The reorder endpoint renumbers every section referenced in a single
    call to a consistent `0..N-1` range (sorted by the caller's requested
    `order`, tie-broken by request-array position for a stable result)
    rather than trusting caller-supplied values are pre-deduplicated -
    nothing at the DB layer enforces `order` uniqueness by design (see that
    same docstring for why: transient duplicate `order` values mid-reorder
    are harmless).
  - DELETE unconditionally rejects `type == "tool_grid"` - the partial
    unique index on `type` (app/core/database.py::ensure_indexes) is a
    backstop, not a substitute for this application-layer check.
"""
import logging
from datetime import datetime
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from pydantic import ValidationError
from pymongo import UpdateOne
from pymongo.errors import DuplicateKeyError

from app.core.database import db
from app.schemas.homepage_section import (
    HomepageSectionBase,
    HomepageSectionCreate,
    HomepageSectionDocument,
    HomepageSectionReorderItem,
    HomepageSectionUpdate,
    SectionType,
)

logger = logging.getLogger(__name__)


class HomepageSectionNotFound(Exception):
    """Raised when a section id doesn't resolve to any `homepage_sections`
    document (including a structurally-invalid ObjectId string)."""


class HomepageSectionContentInvalid(Exception):
    """Raised when a merged update would fail `HomepageSectionBase`'s
    per-type `content` validation. Carries the original validation message
    (already scrubbed by pydantic to just field names/types, never raw
    request internals) so the router can surface a safe 422."""


class ToolGridDeleteForbidden(Exception):
    """Raised on any attempt to delete the single structural `tool_grid`
    section - see this module's docstring."""


def _to_object_id(section_id: str) -> Optional[ObjectId]:
    try:
        return ObjectId(section_id)
    except (InvalidId, TypeError):
        return None


async def list_public_sections() -> list[HomepageSectionDocument]:
    """Only `enabled: true` sections, sorted by `order` - backs the
    unauthenticated `GET /v1/admin/homepage-sections` route."""
    cursor = db.homepage_sections.find({"enabled": True}).sort("order", 1)
    return [HomepageSectionDocument(**doc) async for doc in cursor]


async def list_all_sections() -> list[HomepageSectionDocument]:
    """Every section (enabled or not), sorted by `order` - backs
    `GET /v1/admin/homepage-sections/all` (require_admin)."""
    cursor = db.homepage_sections.find({}).sort("order", 1)
    return [HomepageSectionDocument(**doc) async for doc in cursor]


async def get_section(section_id: str) -> HomepageSectionDocument:
    oid = _to_object_id(section_id)
    if oid is None:
        raise HomepageSectionNotFound(section_id)
    doc = await db.homepage_sections.find_one({"_id": oid})
    if doc is None:
        raise HomepageSectionNotFound(section_id)
    return HomepageSectionDocument(**doc)


async def create_section(payload: HomepageSectionCreate) -> HomepageSectionDocument:
    """`payload` has already round-tripped through `HomepageSectionBase`'s
    per-type content validation (it's the base class) by the time FastAPI
    hands it here - this only owns the actual Mongo write.

    `created_at`/`updated_at` are always stamped fresh here, never taken
    from `payload` as-is - `HomepageSectionCreate.created_at`/`updated_at`
    only default to "now" when the caller omits them; since this is also
    reachable from `POST /v1/admin/homepage-sections` (an admin-authored
    request body, not just the trusted seed script), an admin client could
    otherwise pass an arbitrary timestamp through untouched.

    A second `type: "tool_grid"` create attempt is rejected by the
    `homepage_sections_type_tool_grid_unique` partial index
    (app/core/database.py) - caught here and re-raised as a clean
    `ValueError` rather than letting a raw `DuplicateKeyError`/500 reach
    the client.
    """
    now = datetime.utcnow()
    insert_doc = payload.model_dump()
    insert_doc["created_at"] = now
    insert_doc["updated_at"] = now
    try:
        insert_result = await db.homepage_sections.insert_one(insert_doc)
    except DuplicateKeyError as exc:
        logger.warning("Rejected duplicate tool_grid create attempt: %s", exc)
        raise ValueError("A tool_grid section already exists; only one may exist at a time") from exc
    doc = await db.homepage_sections.find_one({"_id": insert_result.inserted_id})
    logger.info("Created homepage_sections document id=%s type=%s", insert_result.inserted_id, payload.type.value)
    return HomepageSectionDocument(**doc)


async def update_section(section_id: str, update: HomepageSectionUpdate) -> HomepageSectionDocument:
    """Merges `update`'s given fields onto the existing document, then
    re-validates the *merged* result through `HomepageSectionBase` before
    writing anything - never accepts `update.content` as already valid on
    its own (see this module's docstring)."""
    existing = await get_section(section_id)  # raises HomepageSectionNotFound

    merged_content = update.content if update.content is not None else existing.content
    merged_order = update.order if update.order is not None else existing.order
    merged_enabled = update.enabled if update.enabled is not None else existing.enabled

    try:
        validated = HomepageSectionBase(
            type=existing.type,
            order=merged_order,
            enabled=merged_enabled,
            content=merged_content,
        )
    except ValidationError as exc:
        logger.warning("Rejected invalid content on update for section %s: %s", section_id, exc)
        raise HomepageSectionContentInvalid(str(exc)) from exc

    update_doc = {
        "order": validated.order,
        "enabled": validated.enabled,
        "content": validated.content,
        "updated_at": datetime.utcnow(),
    }
    await db.homepage_sections.update_one({"_id": existing.id}, {"$set": update_doc})
    doc = await db.homepage_sections.find_one({"_id": existing.id})
    logger.info("Updated homepage_sections document id=%s", section_id)
    return HomepageSectionDocument(**doc)


async def reorder_sections(items: list[HomepageSectionReorderItem]) -> list[HomepageSectionDocument]:
    """Renumbers every section referenced in `items` to a consistent
    `0..N-1` range, sorted by the caller's requested `order` (ties broken
    by the item's position in the request list, for a stable result) -
    never trusts the caller-supplied `order` values directly, since nothing
    at the DB layer enforces their uniqueness (see this module's
    docstring)."""
    object_ids: list[ObjectId] = []
    for item in items:
        oid = _to_object_id(item.id)
        if oid is None:
            raise HomepageSectionNotFound(item.id)
        object_ids.append(oid)

    existing_count = await db.homepage_sections.count_documents({"_id": {"$in": object_ids}})
    if existing_count != len(object_ids):
        raise HomepageSectionNotFound("one or more section ids in the reorder request do not exist")

    ranked = sorted(
        range(len(items)),
        key=lambda idx: (items[idx].order, idx),
    )
    new_order_by_id = {object_ids[idx]: position for position, idx in enumerate(ranked)}

    operations = [
        UpdateOne({"_id": oid}, {"$set": {"order": new_order, "updated_at": datetime.utcnow()}})
        for oid, new_order in new_order_by_id.items()
    ]
    if operations:
        await db.homepage_sections.bulk_write(operations, ordered=False)
    logger.info("Reordered %d homepage_sections documents", len(operations))

    return await list_all_sections()


async def delete_section(section_id: str) -> None:
    existing = await get_section(section_id)  # raises HomepageSectionNotFound
    if existing.type == SectionType.TOOL_GRID:
        logger.warning("Rejected delete attempt on the structural tool_grid section %s", section_id)
        raise ToolGridDeleteForbidden(section_id)
    await db.homepage_sections.delete_one({"_id": existing.id})
    logger.info("Deleted homepage_sections document id=%s", section_id)
