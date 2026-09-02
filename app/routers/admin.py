"""`admin` module HTTP surface (ADR-019) - Homepage Sections CMS.

First code in the `admin` module (Handbook Part C.3's 11-module list).
Depends on `auth` per the module dependency chain (`auth -> user -> file
-> job -> pdf/image/ai`; `admin` sits alongside/after `auth` the same way
`app/core/admin_auth.py::require_admin` already does) - never the reverse.

All service logic (Mongo reads/writes, per-type content re-validation on
update, reorder renumbering, tool_grid delete protection) lives in
`app/services/admin/homepage_sections_service.py`; this file only does
HTTP concerns - request/response shaping, auth wiring, and translating
service-layer exceptions into `app.shared.responses.api_error(...)`.

Two APIRouter instances, deliberately, both mounted at the same
`/admin/homepage-sections` prefix in `app/main.py`:

- `public_router` - exactly one route, `GET /v1/admin/homepage-sections`
  (enabled sections only). Registered in `app/main.py` WITHOUT
  `protected_dependency` - no `x-api-key`, no admin session, by spec (the
  public homepage renders this without any credential).
- `router` - every other route (list-all, create, update, reorder,
  delete). Registered in `app/main.py` WITH `protected_dependency`
  (`verify_api_key` + rate limiting), the same router-level default every
  other tool router gets - `admin` is not given the bare-router exemption
  `auth.router` has, because `admin` has no login-chicken-and-egg problem
  to justify it (see app/main.py's include_router comment for the full
  reasoning). Each individual route on `router` ALSO carries its own
  `Depends(require_admin)` - two independent auth layers on every write,
  not one, so a route that ever forgot `require_admin` would still be
  blocked by the router-level `x-api-key` check (ADR-008 Secure by
  Default).

Why two `APIRouter` instances instead of one: FastAPI's `include_router(...,
dependencies=[...])` appends those dependencies to every route already
defined on that router at inclusion time - there is no per-route "opt out
of the router-level list" switch once a router-wide dependencies list is
supplied at `include_router()`. Splitting the one public route onto its own
router (mounted without `protected_dependency`) is the idiomatic way to
give a single route a genuinely different dependency chain than its
siblings under the same path prefix, confirmed against this codebase's
FastAPI 0.115.14.
"""
import logging

from fastapi import APIRouter, Depends

from app.core.admin_auth import require_admin
from app.schemas.homepage_section import (
    HomepageSectionCreate,
    HomepageSectionReorderRequest,
    HomepageSectionUpdate,
)
from app.services.admin.homepage_sections_service import (
    HomepageSectionContentInvalid,
    HomepageSectionNotFound,
    ToolGridDeleteForbidden,
    create_section,
    delete_section,
    list_all_sections,
    list_public_sections,
    reorder_sections,
    update_section,
)
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

_PREFIX = "/admin/homepage-sections"

public_router = APIRouter(prefix=_PREFIX, tags=["Admin"])
router = APIRouter(prefix=_PREFIX, tags=["Admin"])


def _section_out(section) -> dict:
    return {
        "id": str(section.id),
        "type": section.type.value,
        "order": section.order,
        "enabled": section.enabled,
        "content": section.content,
        "created_at": section.created_at.isoformat(),
        "updated_at": section.updated_at.isoformat(),
    }


@public_router.get("", summary="Public: list enabled homepage sections, sorted by order")
async def get_public_sections():
    sections = await list_public_sections()
    logger.debug("Listed %d public homepage sections", len(sections))
    return envelope(True, "Homepage sections retrieved", data=[_section_out(s) for s in sections])


@router.get("/all", summary="Admin: list every homepage section, including disabled")
async def get_all_sections(admin: dict = Depends(require_admin)):
    sections = await list_all_sections()
    logger.debug("Admin %s listed %d homepage sections (all)", admin.get("email"), len(sections))
    return envelope(True, "Homepage sections retrieved", data=[_section_out(s) for s in sections])


@router.post("", summary="Admin: create a homepage section")
async def create_homepage_section(body: HomepageSectionCreate, admin: dict = Depends(require_admin)):
    try:
        section = await create_section(body)
    except ValueError as exc:
        logger.warning("Create homepage section failed for admin %s: %s", admin.get("email"), exc)
        raise api_error(409, str(exc), "HOMEPAGE_SECTION_CONFLICT") from exc
    logger.info("Admin %s created homepage section %s (type=%s)", admin.get("email"), section.id, section.type.value)
    return envelope(True, "Homepage section created", data=_section_out(section))


@router.put("/{section_id}", summary="Admin: edit a homepage section's content/order/enabled state")
async def update_homepage_section(
    section_id: str, body: HomepageSectionUpdate, admin: dict = Depends(require_admin)
):
    try:
        section = await update_section(section_id, body)
    except HomepageSectionNotFound as exc:
        logger.warning("Update failed, section not found: %s", section_id)
        raise api_error(404, "Homepage section not found", "HOMEPAGE_SECTION_NOT_FOUND") from exc
    except HomepageSectionContentInvalid as exc:
        logger.warning("Update rejected, invalid content for section %s: %s", section_id, exc)
        raise api_error(422, "Invalid content for this section's type", "HOMEPAGE_SECTION_CONTENT_INVALID") from exc
    logger.info("Admin %s updated homepage section %s", admin.get("email"), section_id)
    return envelope(True, "Homepage section updated", data=_section_out(section))


@router.post("/reorder", summary="Admin: bulk reorder homepage sections (renormalized to 0..N-1)")
async def reorder_homepage_sections(body: HomepageSectionReorderRequest, admin: dict = Depends(require_admin)):
    try:
        sections = await reorder_sections(body.sections)
    except HomepageSectionNotFound as exc:
        logger.warning("Reorder failed, unknown section id(s): %s", exc)
        raise api_error(404, "One or more section ids were not found", "HOMEPAGE_SECTION_NOT_FOUND") from exc
    logger.info("Admin %s reordered %d homepage sections", admin.get("email"), len(body.sections))
    return envelope(True, "Homepage sections reordered", data=[_section_out(s) for s in sections])


@router.delete("/{section_id}", summary="Admin: delete a homepage section (tool_grid is never deletable)")
async def delete_homepage_section(section_id: str, admin: dict = Depends(require_admin)):
    try:
        await delete_section(section_id)
    except HomepageSectionNotFound as exc:
        logger.warning("Delete failed, section not found: %s", section_id)
        raise api_error(404, "Homepage section not found", "HOMEPAGE_SECTION_NOT_FOUND") from exc
    except ToolGridDeleteForbidden as exc:
        logger.warning("Delete rejected for tool_grid section: %s", section_id)
        raise api_error(400, "The tool_grid section cannot be deleted", "TOOL_GRID_DELETE_FORBIDDEN") from exc
    logger.info("Admin %s deleted homepage section %s", admin.get("email"), section_id)
    return envelope(True, "Homepage section deleted", data=None)
