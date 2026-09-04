"""`content` module HTTP surface (ADR-021) - Content Model Foundation.

First code in the new `content` module (sibling to `admin`, per ADR-021's
module-boundary decision). Depends on `auth` the same way `admin` does -
reuses `app.core.admin_auth.require_admin` unmodified, no new auth
mechanism. `content` and `admin` never depend on each other in either
direction (ADR-021).

All service logic (Mongo reads/writes, the `content_type='tool_metadata'`
read-only invariant, reorder renumbering, tag normalize-and-upsert) lives in
`app/services/content/categories_service.py` and
`app/services/content/tags_service.py`; this file only does HTTP concerns -
request/response shaping, auth wiring, and translating service-layer
exceptions into `app.shared.responses.api_error(...)`. Same division of
responsibility `app/routers/admin.py` documents for `homepage_sections`.

Two `APIRouter` instances, deliberately, both mounted at the same `/content`
prefix in `app/main.py` - exact precedent from `app/routers/admin.py`
(read that module's docstring for the full "why two routers" reasoning,
not repeated here):

- `public_router` - unauthenticated reads only: `GET /v1/content/categories`
  (optional `?content_type=` filter) and `GET /v1/content/tags` (for a
  future CMS admin UI's typeahead). Registered in `app/main.py` WITHOUT
  `protected_dependency`, exactly like `admin.public_router`.
- `router` - every admin-write route: create/update/delete/reorder
  categories. Registered in `app/main.py` WITH `protected_dependency`
  (`verify_api_key` + rate limiting), the same router-level default every
  other tool router gets. Each individual route on `router` ALSO carries
  its own `Depends(require_admin)` - two independent auth layers on every
  write (ADR-008 Secure by Default), not one, matching `admin.py` exactly.

There is deliberately no direct CRUD route for `tags` here - see
`app/services/content/tags_service.py`'s module docstring: the only writer
is `get_or_create_tag`, called by the future Tools Metadata CMS / Blog CMS
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
