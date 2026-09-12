"""`content_pages` collection CRUD for the `content` module (ADR-021's
foundation, extended by ADR-022: Dynamic CMS Pages - content-block model and
route-collision handling, `docs/architecture/adr/ADR-022-dynamic-pages-content-model-and-routing.md`
- see `app/schemas/content_page.py`'s module docstring for the full feature
background).

Owns every read/write against `db.content_pages`, mirroring
`blog_posts_service.py`'s/`tool_metadata_service.py`'s "one module owns its
collection" pattern (Handbook Part C.3). Called by `app/routers/content.py` -
no HTTP concerns here (no `HTTPException`), same convention as those two
modules: raises plain exception classes the router translates into
`app.shared.responses.api_error(...)`.

**`slug` immutability**: `ContentPageUpdate` has no `slug` field at all
(enforced at the schema layer already, see `content_page.py`'s module
docstring) - this service never accepts or writes a slug change on update,
so `SlugReserved` is only ever raised on create.

**Reserved-slug rejection**: `create_page` checks the incoming `slug` against
`content_page.RESERVED_TOP_LEVEL_SLUGS` (imported, not redefined - that
module is the single canonical source, see its own docstring) before ever
attempting the insert - a cheap in-memory set membership check, no DB round
trip needed. Per ADR-022's decision this must fail loud at create time, not
be discovered later as a silent route shadow.

**`published_at` stamping** (see `content_page.py`'s module docstring and
`content_blog_post.py`'s module docstring for the full reasoning - identical
semantics, only the mechanical rule is restated here):
- `create_page`: if the create body's `status` is already `PUBLISHED` at
  creation time (a page can be born already-published), stamp
  `published_at = utcnow()` on insert. Otherwise leave it `None`.
- `update_page`: stamp `published_at = utcnow()` if and only if ALL of the
  following hold: (1) the incoming `body.status` is `PUBLISHED`, (2) the
  *existing* document's `status` is NOT already `PUBLISHED` (a genuine
  draft->published transition, not a no-op re-save of an already-published
  page), and (3) the existing document's `published_at` is still `None`
  (this page has never been published before). In every other case -
  already published, unpublishing, or re-publishing something that was
  published at some point in the past - `published_at` is left completely
  untouched. `updated_at` is always stamped regardless.

**Public vs admin read visibility**: `get_by_slug(slug, include_drafts=False)`
(the public route's call) must make a draft look identical to "this slug
does not exist" - raising the same `PageNotFound` a truly-missing slug would
raise, never leaking that a draft exists under that slug. Only the admin
route passes `include_drafts=True`.

**No pagination on `list_all_pages`** - unlike `blog_posts_service.list_posts`,
this collection has no paginated/filtered query anywhere in the approved
spec (see `content_page.py`'s module docstring's "Indexing decisions"
section for the full reasoning). Mirrors `tool_metadata_service.list_all()`'s
unpaginated shape instead, sorted by `created_at` descending (a sane default
for an admin "all pages" view - newest-created first).
"""
import logging
from datetime import datetime

from pymongo.errors import DuplicateKeyError

from app.core.database import db
from app.schemas.content_page import (
    RESERVED_TOP_LEVEL_SLUGS,
    ContentPageCreate,
    ContentPageDocument,
    ContentPageUpdate,
    PageStatus,
)

logger = logging.getLogger(__name__)


class PageNotFound(Exception):
    """Raised when a `slug` doesn't resolve to any `content_pages` document -
    also raised by the public-facing `get_by_slug(include_drafts=False)`
    call when the slug exists but is a draft, so a draft is indistinguishable
    from a missing page to an unauthenticated caller (see module docstring)."""


class PageSlugConflict(Exception):
    """Raised when a create would collide with an existing `slug` - the
    `content_pages_slug_unique` index (app/core/database.py) is the actual
    guarantee; this is a clean, client-safe translation of the resulting
    `DuplicateKeyError`."""


class SlugReserved(Exception):
    """Raised when a create body's `slug` is a member of
    `content_page.RESERVED_TOP_LEVEL_SLUGS` - a fail-loud rejection at
    create time, per ADR-022's decision (see module docstring)."""


async def get_by_slug(slug: str, *, include_drafts: bool = False) -> ContentPageDocument:
    """Backs both the public `GET /v1/content/pages/{slug}` route
    (`include_drafts=False`) and the admin equivalent (`include_drafts=True`).

    With `include_drafts=False`, a draft page's slug raises `PageNotFound`
    exactly like a genuinely-missing slug would - see module docstring for
    why this must never leak draft existence to an unauthenticated caller.
    """
    doc = await db.content_pages.find_one({"slug": slug})
    if doc is None:
        raise PageNotFound(slug)
    page = ContentPageDocument(**doc)
    if not include_drafts and page.status != PageStatus.PUBLISHED:
        raise PageNotFound(slug)
    return page


async def list_all_pages() -> list[ContentPageDocument]:
    """Every `content_pages` document, regardless of status, sorted by
    `created_at` descending - backs the admin `GET /v1/content/admin/pages`
    route. Deliberately unpaginated, mirroring `tool_metadata_service.list_all()`'s
    shape rather than `blog_posts_service.list_posts()`'s paginated one - see
    `content_page.py`'s module docstring's "Indexing decisions" section for
    the full reasoning (no paginated/filtered query anywhere in the approved
    spec for this collection)."""
    cursor = db.content_pages.find({}).sort("created_at", -1)
    return [ContentPageDocument(**doc) async for doc in cursor]


async def create_page(body: ContentPageCreate) -> ContentPageDocument:
    if body.slug in RESERVED_TOP_LEVEL_SLUGS:
        logger.warning("Rejected content_pages create, reserved slug: %s", body.slug)
        raise SlugReserved(body.slug)

    now = datetime.utcnow()
    insert_doc = body.model_dump()
    # Never trust caller-supplied created_at/updated_at (both are real fields
    # on ContentPageCreate, so extra="forbid" doesn't block a client from
    # setting them) - always stamp fresh server-side, mirroring
    # blog_posts_service.create_post's identical override.
    insert_doc["created_at"] = now
    insert_doc["updated_at"] = now
    # A page can be born already-published (status="published" at create
    # time) - stamp published_at once, at insert, in that case. See module
    # docstring's published_at section.
    insert_doc["published_at"] = now if body.status == PageStatus.PUBLISHED else None

    try:
        insert_result = await db.content_pages.insert_one(insert_doc)
    except DuplicateKeyError as exc:
        logger.warning("Rejected duplicate content_pages slug on create: %s", body.slug)
        raise PageSlugConflict(body.slug) from exc

    doc = await db.content_pages.find_one({"_id": insert_result.inserted_id})
    logger.info("Created content_pages document id=%s slug=%s", insert_result.inserted_id, body.slug)
    return ContentPageDocument(**doc)


async def update_page(slug: str, body: ContentPageUpdate) -> ContentPageDocument:
    # include_drafts=True: an admin editing a page must be able to load it
    # regardless of its current status.
    existing = await get_by_slug(slug, include_drafts=True)  # raises PageNotFound

    update_doc: dict = {"updated_at": datetime.utcnow()}
    if body.title is not None:
        update_doc["title"] = body.title
    if body.meta_description is not None:
        update_doc["meta_description"] = body.meta_description
    if body.blocks is not None:
        update_doc["blocks"] = [block.model_dump() for block in body.blocks]
    if body.status is not None:
        update_doc["status"] = body.status.value
        # published_at stamping: only on a genuine first-ever draft->published
        # transition (see module docstring's published_at section for the
        # full reasoning). All three conditions must hold:
        #   1. this update is setting status to PUBLISHED,
        #   2. the existing document isn't already PUBLISHED (otherwise this
        #      is a no-op re-save, not a transition), and
        #   3. the existing document has never been published before
        #      (published_at is still None).
        # Every other case (already published, unpublishing, or
        # re-publishing something published in the past) leaves published_at
        # completely untouched.
        if (
            body.status == PageStatus.PUBLISHED
            and existing.status != PageStatus.PUBLISHED
            and existing.published_at is None
        ):
            update_doc["published_at"] = datetime.utcnow()

    await db.content_pages.update_one({"_id": existing.id}, {"$set": update_doc})

    doc = await db.content_pages.find_one({"_id": existing.id})
    logger.info("Updated content_pages document slug=%s", slug)
    return ContentPageDocument(**doc)


async def delete_page(slug: str) -> None:
    existing = await get_by_slug(slug, include_drafts=True)  # raises PageNotFound
    await db.content_pages.delete_one({"_id": existing.id})
    logger.info("Deleted content_pages document slug=%s", slug)
