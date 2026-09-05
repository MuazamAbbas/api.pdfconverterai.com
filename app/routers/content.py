"""`content` module HTTP surface (ADR-021) - Content Model Foundation.

First code in the new `content` module (sibling to `admin`, per ADR-021's
module-boundary decision). Depends on `auth` the same way `admin` does -
reuses `app.core.admin_auth.require_admin` unmodified, no new auth
mechanism. `content` and `admin` never depend on each other in either
direction (ADR-021).

Also carries the Tools Metadata CMS routes (feature spec approved
2026-09-04, see `docs/roadmap/SPRINT_STATUS.md`'s 2026-09-04 entry): public
`GET /v1/content/tool-metadata/{slug}` and admin
`POST/GET/PUT/DELETE /v1/content/tool-metadata[/{slug}]`, backed by
`app/services/content/tool_metadata_service.py`. Same `content` module,
same `/content` prefix, same two-router split - not a new module or a new
router registration.

All service logic (Mongo reads/writes, the `content_type='tool_metadata'`
read-only invariant, reorder renumbering, tag normalize-and-upsert,
tool-metadata category/tag validation) lives in
`app/services/content/categories_service.py`,
`app/services/content/tags_service.py`, and
`app/services/content/tool_metadata_service.py`; this file only does HTTP
concerns - request/response shaping, auth wiring, and translating
service-layer exceptions into `app.shared.responses.api_error(...)`. Same
division of responsibility `app/routers/admin.py` documents for
`homepage_sections`.

Two `APIRouter` instances, deliberately, both mounted at the same `/content`
prefix in `app/main.py` - exact precedent from `app/routers/admin.py`
(read that module's docstring for the full "why two routers" reasoning,
not repeated here):

- `public_router` - unauthenticated reads only: `GET /v1/content/categories`
  (optional `?content_type=` filter), `GET /v1/content/tags` (for a future
  CMS admin UI's typeahead), and `GET /v1/content/tool-metadata/{slug}`.
  Registered in `app/main.py` WITHOUT `protected_dependency`, exactly like
  `admin.public_router`.
- `router` - every admin-write route: create/update/delete/reorder
  categories, and create/list/update/delete tool metadata. Registered in
  `app/main.py` WITH `protected_dependency` (`verify_api_key` + rate
  limiting), the same router-level default every other tool router gets.
  Each individual route on `router` ALSO carries its own
  `Depends(require_admin)` - two independent auth layers on every write
  (ADR-008 Secure by Default), not one, matching `admin.py` exactly.

There is deliberately no direct CRUD route for `tags` here - see
`app/services/content/tags_service.py`'s module docstring: the only writer
is `get_or_create_tag`, called by the Tools Metadata CMS / future Blog CMS
routers when they attach tags to a content item, never by an admin typing a
tag directly through this API.
"""
import logging

from fastapi import APIRouter, Depends

from app.core.admin_auth import require_admin
from app.schemas.content_category import (
    ContentCategoryCreate,
    ContentCategoryReorderRequest,
    ContentCategoryUpdate,
    ContentType,
)
from app.schemas.content_tool_metadata import (
    ContentToolMetadataCreate,
    ContentToolMetadataUpdate,
)
from app.services.content.categories_service import (
    CategoryNotFound,
    CategoryReadOnly,
    CategorySlugConflict,
    create_category,
    delete_category,
    list_categories,
    reorder_categories,
    update_category,
)
from app.services.content.tags_service import list_tags
from app.services.content.tool_metadata_service import (
    InvalidCategory,
    ToolMetadataNotFound,
    ToolMetadataSlugConflict,
    create_tool_metadata,
    delete_tool_metadata,
    get_by_slug,
    list_all,
    update_tool_metadata,
)
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

_PREFIX = "/content"

public_router = APIRouter(prefix=_PREFIX, tags=["Content"])
router = APIRouter(prefix=_PREFIX, tags=["Content"])


def _category_out(category) -> dict:
    return {
        "id": str(category.id),
        "label": category.label,
        "slug": category.slug,
        "content_type": category.content_type.value,
        "color_token": category.color_token,
        "order": category.order,
        "created_at": category.created_at.isoformat(),
        "updated_at": category.updated_at.isoformat(),
    }


def _tag_out(tag) -> dict:
    return {
        "id": str(tag.id),
        "slug": tag.slug,
        "label": tag.label,
        "created_at": tag.created_at.isoformat(),
    }


def _tool_metadata_out(tool_metadata) -> dict:
    return {
        "id": str(tool_metadata.id),
        "slug": tool_metadata.slug,
        "title": tool_metadata.title,
        "category": tool_metadata.category,
        "icon": tool_metadata.icon,
        "description": tool_metadata.description,
        "tags": tool_metadata.tags,
        "how_to_use": tool_metadata.how_to_use,
        "faq": tool_metadata.faq,
        "ad_slot": tool_metadata.ad_slot.model_dump() if tool_metadata.ad_slot is not None else None,
        "created_at": tool_metadata.created_at.isoformat(),
        "updated_at": tool_metadata.updated_at.isoformat(),
    }


@public_router.get(
    "/categories",
    summary="Public: list content categories, optionally filtered by content_type",
)
async def get_public_categories(content_type: ContentType | None = None):
    categories = await list_categories(content_type=content_type)
    logger.debug("Listed %d content categories (content_type=%s)", len(categories), content_type)
    return envelope(True, "Content categories retrieved", data=[_category_out(c) for c in categories])


@public_router.get(
    "/tags",
    summary="Public: list every tag (for CMS admin UI autocomplete)",
)
async def get_public_tags():
    tags = await list_tags()
    logger.debug("Listed %d tags", len(tags))
    return envelope(True, "Tags retrieved", data=[_tag_out(t) for t in tags])


@public_router.get(
    "/tool-metadata/{slug}",
    summary="Public: fetch one tool's editable marketing/SEO metadata by slug",
)
async def get_public_tool_metadata(slug: str):
    try:
        tool_metadata = await get_by_slug(slug)
    except ToolMetadataNotFound as exc:
        # Expected/normal: a tool with no CMS row yet is the common case,
        # not an error condition - the frontend falls back to
        # tools-registry.ts on any 404 here (spec AC2). Debug only, never
        # warning/error.
        logger.debug("No content_tool_metadata row for slug=%s: %s", slug, exc)
        raise api_error(404, "Tool metadata not found", "TOOL_METADATA_NOT_FOUND") from exc
    logger.debug("Retrieved content_tool_metadata for slug=%s", slug)
    return envelope(True, "Tool metadata retrieved", data=_tool_metadata_out(tool_metadata))


@router.post(
    "/categories",
    summary="Admin: create a blog content category (tool_metadata categories are seed-only)",
)
async def create_content_category(body: ContentCategoryCreate, admin: dict = Depends(require_admin)):
    try:
        category = await create_category(body)
    except CategoryReadOnly as exc:
        logger.warning("Create rejected, read-only content_type for admin %s: %s", admin.get("email"), exc)
        raise api_error(
            400, "tool_metadata categories cannot be created through this API", "CATEGORY_READ_ONLY"
        ) from exc
    except CategorySlugConflict as exc:
        logger.warning("Create rejected, duplicate slug for admin %s: %s", admin.get("email"), exc)
        raise api_error(409, "A category with this slug already exists", "CATEGORY_SLUG_CONFLICT") from exc
    logger.info("Admin %s created content category %s (slug=%s)", admin.get("email"), category.id, category.slug)
    return envelope(True, "Content category created", data=_category_out(category))


@router.put(
    "/categories/{category_id}",
    summary="Admin: edit a blog content category's label/slug/order",
)
async def update_content_category(
    category_id: str, body: ContentCategoryUpdate, admin: dict = Depends(require_admin)
):
    try:
        category = await update_category(category_id, body)
    except CategoryNotFound as exc:
        logger.warning("Update failed, category not found: %s", category_id)
        raise api_error(404, "Content category not found", "CATEGORY_NOT_FOUND") from exc
    except CategoryReadOnly as exc:
        logger.warning("Update rejected on read-only tool_metadata category %s: %s", category_id, exc)
        raise api_error(400, "tool_metadata categories cannot be edited", "CATEGORY_READ_ONLY") from exc
    except CategorySlugConflict as exc:
        logger.warning("Update rejected, duplicate slug for category %s: %s", category_id, exc)
        raise api_error(409, "A category with this slug already exists", "CATEGORY_SLUG_CONFLICT") from exc
    logger.info("Admin %s updated content category %s", admin.get("email"), category_id)
    return envelope(True, "Content category updated", data=_category_out(category))


@router.post(
    "/categories/reorder",
    summary="Admin: bulk reorder blog content categories (renormalized to 0..N-1)",
)
async def reorder_content_categories(body: ContentCategoryReorderRequest, admin: dict = Depends(require_admin)):
    try:
        categories = await reorder_categories(body.categories)
    except CategoryNotFound as exc:
        logger.warning("Reorder failed, unknown category id(s): %s", exc)
        raise api_error(404, "One or more category ids were not found", "CATEGORY_NOT_FOUND") from exc
    except CategoryReadOnly as exc:
        logger.warning("Reorder rejected, one or more read-only tool_metadata categories: %s", exc)
        raise api_error(400, "tool_metadata categories cannot be reordered", "CATEGORY_READ_ONLY") from exc
    logger.info("Admin %s reordered %d content categories", admin.get("email"), len(body.categories))
    return envelope(True, "Content categories reordered", data=[_category_out(c) for c in categories])


@router.delete(
    "/categories/{category_id}",
    summary="Admin: delete a blog content category (tool_metadata categories are never deletable)",
)
async def delete_content_category(category_id: str, admin: dict = Depends(require_admin)):
    try:
        await delete_category(category_id)
    except CategoryNotFound as exc:
        logger.warning("Delete failed, category not found: %s", category_id)
        raise api_error(404, "Content category not found", "CATEGORY_NOT_FOUND") from exc
    except CategoryReadOnly as exc:
        logger.warning("Delete rejected on read-only tool_metadata category %s: %s", category_id, exc)
        raise api_error(400, "tool_metadata categories cannot be deleted", "CATEGORY_READ_ONLY") from exc
    logger.info("Admin %s deleted content category %s", admin.get("email"), category_id)
    return envelope(True, "Content category deleted", data=None)


@router.get(
    "/tool-metadata",
    summary="Admin: list every tool's editable marketing/SEO metadata",
)
async def list_admin_tool_metadata(admin: dict = Depends(require_admin)):
    tool_metadata_list = await list_all()
    logger.info("Admin %s listed %d content_tool_metadata rows", admin.get("email"), len(tool_metadata_list))
    return envelope(
        True, "Tool metadata retrieved", data=[_tool_metadata_out(t) for t in tool_metadata_list]
    )


@router.post(
    "/tool-metadata",
    summary="Admin: create a tool's editable marketing/SEO metadata row",
)
async def create_admin_tool_metadata(body: ContentToolMetadataCreate, admin: dict = Depends(require_admin)):
    try:
        tool_metadata = await create_tool_metadata(body)
    except InvalidCategory as exc:
        logger.warning("Create rejected, invalid category for admin %s: %s", admin.get("email"), exc)
        raise api_error(400, "category must be an existing tool_metadata content category", "INVALID_CATEGORY") from exc
    except ToolMetadataSlugConflict as exc:
        logger.warning("Create rejected, duplicate slug for admin %s: %s", admin.get("email"), exc)
        raise api_error(
            409, "A tool metadata row with this slug already exists", "TOOL_METADATA_SLUG_CONFLICT"
        ) from exc
    logger.info(
        "Admin %s created content_tool_metadata %s (slug=%s)", admin.get("email"), tool_metadata.id, tool_metadata.slug
    )
    return envelope(True, "Tool metadata created", data=_tool_metadata_out(tool_metadata))


@router.put(
    "/tool-metadata/{slug}",
    summary="Admin: edit a tool's editable marketing/SEO metadata (slug is immutable)",
)
async def update_admin_tool_metadata(
    slug: str, body: ContentToolMetadataUpdate, admin: dict = Depends(require_admin)
):
    try:
        tool_metadata = await update_tool_metadata(slug, body)
    except ToolMetadataNotFound as exc:
        logger.warning("Update failed, tool metadata not found: %s", slug)
        raise api_error(404, "Tool metadata not found", "TOOL_METADATA_NOT_FOUND") from exc
    except InvalidCategory as exc:
        logger.warning("Update rejected, invalid category for slug=%s: %s", slug, exc)
        raise api_error(400, "category must be an existing tool_metadata content category", "INVALID_CATEGORY") from exc
    logger.info("Admin %s updated content_tool_metadata slug=%s", admin.get("email"), slug)
    return envelope(True, "Tool metadata updated", data=_tool_metadata_out(tool_metadata))


@router.delete(
    "/tool-metadata/{slug}",
    summary="Admin: delete a tool's editable marketing/SEO metadata row",
)
async def delete_admin_tool_metadata(slug: str, admin: dict = Depends(require_admin)):
    try:
        await delete_tool_metadata(slug)
    except ToolMetadataNotFound as exc:
        logger.warning("Delete failed, tool metadata not found: %s", slug)
        raise api_error(404, "Tool metadata not found", "TOOL_METADATA_NOT_FOUND") from exc
    logger.info("Admin %s deleted content_tool_metadata slug=%s", admin.get("email"), slug)
    return envelope(True, "Tool metadata deleted", data=None)
