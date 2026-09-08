"""`content_blog_posts` collection CRUD for the `content` module (ADR-021's
foundation, Blog/News CMS - direct extension of the just-shipped Tools
Metadata CMS, see `app/schemas/content_blog_post.py`'s module docstring for
the full feature background).

Owns every read/write against `db.content_blog_posts`, mirroring
`tool_metadata_service.py`'s "one module owns its collection" pattern
(Handbook Part C.3). Called by `app/routers/content.py` - no HTTP concerns
here (no `HTTPException`), same convention as `tool_metadata_service.py`:
raises plain exception classes the router translates into
`app.shared.responses.api_error(...)`.

**`category` validation**: every create/update that touches `category`
resolves it against `categories_service.list_categories(ContentType.BLOG)` -
**not** `ContentType.TOOL_METADATA`. This is the exact mistake
`content_blog_post.py`'s module docstring warns about: the two CMS systems
share one `content_categories` collection but validate `category` against
two disjoint, non-overlapping slices of it. A blog post filed under a tool
category (or vice versa) must be rejected with `InvalidCategory` - copying
`tool_metadata_service.py`'s `_validate_category` without changing its
`content_type` filter argument would silently accept the wrong categories
or reject every legitimate one.

**`tags` normalization**: every create/update that touches `tags` runs each
raw string through `tags_service.get_or_create_tag`, replacing the payload's
`tags` list with the returned canonical slugs before the document is
written - never raw admin-typed strings land in `db.content_blog_posts`.
This module keeps its own private `_normalize_tags` helper rather than
importing `tool_metadata_service._normalize_tags` - the two are identical in
behavior today, but they validate/normalize for two independently-evolving
schemas and collections, and sharing a "private" (underscore-prefixed)
helper across modules would create exactly the kind of implicit cross-module
coupling Handbook Part C.3 warns against.

**`slug` immutability**: `ContentBlogPostUpdate` has no `slug` field at all
(enforced at the schema layer already, see that module's docstring) - this
service never accepts or writes a slug change on update.

**`published_at` stamping** (see `content_blog_post.py`'s module docstring
for the full reasoning - only the mechanical rule is restated here):
- `create_post`: if the create body's `status` is already `PUBLISHED` at
  creation time (a post can be born already-published), stamp
  `published_at = utcnow()` on insert. Otherwise leave it `None`.
- `update_post`: stamp `published_at = utcnow()` if and only if ALL of the
  following hold: (1) the incoming `body.status` is `PUBLISHED`, (2) the
  *existing* document's `status` is NOT already `PUBLISHED` (a genuine
  draft->published transition, not a no-op re-save of an already-published
  post), and (3) the existing document's `published_at` is still `None`
  (this post has never been published before). In every other case -
  already published, unpublishing, or re-publishing something that was
  published at some point in the past - `published_at` is left completely
  untouched. `updated_at` is always stamped regardless.

**Public vs admin read visibility**: `get_by_slug(slug, include_drafts=False)`
(the public route's call) must make a draft look identical to "this slug
does not exist" - raising the same `BlogPostNotFound` a truly-missing slug
would raise, never leaking that a draft exists under that slug. Only the
admin route passes `include_drafts=True`.
"""
import logging
from datetime import datetime
from typing import Optional

from pymongo.errors import DuplicateKeyError

from app.core.database import db
from app.schemas.content_blog_post import (
    BlogPostStatus,
    ContentBlogPostCreate,
    ContentBlogPostDocument,
    ContentBlogPostUpdate,
)
from app.schemas.content_category import ContentType
from app.services.content.categories_service import list_categories
from app.services.content.tags_service import get_or_create_tag
from app.shared.pagination import PaginationParams

logger = logging.getLogger(__name__)


class BlogPostNotFound(Exception):
    """Raised when a `slug` doesn't resolve to any `content_blog_posts`
    document - also raised by the public-facing `get_by_slug(include_drafts=False)`
    call when the slug exists but is a draft, so a draft is indistinguishable
    from a missing post to an unauthenticated caller (see module docstring)."""


class BlogPostSlugConflict(Exception):
    """Raised when a create would collide with an existing `slug` - the
    `content_blog_posts_slug_unique` index (app/core/database.py) is the
    actual guarantee; this is a clean, client-safe translation of the
    resulting `DuplicateKeyError`."""


class InvalidCategory(Exception):
    """Raised when `category` does not match any existing `content_categories`
    slug with `content_type='blog'` - see this module's docstring. Not the
    same class as `tool_metadata_service.InvalidCategory` (a separate
    exception in a separate module, deliberately not shared/imported - see
    module docstring), even though both may map to the same
    `INVALID_CATEGORY` error code at the router layer."""


async def _validate_category(category: str) -> None:
    valid_categories = await list_categories(content_type=ContentType.BLOG)
    valid_slugs = {c.slug for c in valid_categories}
    if category not in valid_slugs:
        logger.warning("Rejected content_blog_posts write, unknown category %r", category)
        raise InvalidCategory(category)


async def _normalize_tags(tags: list[str]) -> list[str]:
    canonical: list[str] = []
    for raw_tag in tags:
        tag_doc = await get_or_create_tag(raw_tag)
        canonical.append(tag_doc.slug)
    # De-duplicate while preserving first-seen order - e.g. raw tags
    # ["News", "news"] both correctly resolve to the same canonical `news`
    # tags-collection document, but without this the content_blog_posts row
    # itself would store ["news", "news"].
    return list(dict.fromkeys(canonical))


async def get_by_slug(slug: str, *, include_drafts: bool = False) -> ContentBlogPostDocument:
    """Backs both the public `GET /v1/content/blog-posts/{slug}` route
    (`include_drafts=False`) and the admin equivalent (`include_drafts=True`).

    With `include_drafts=False`, a draft post's slug raises `BlogPostNotFound`
    exactly like a genuinely-missing slug would - see module docstring for
    why this must never leak draft existence to an unauthenticated caller.
    """
    doc = await db.content_blog_posts.find_one({"slug": slug})
    if doc is None:
        raise BlogPostNotFound(slug)
    post = ContentBlogPostDocument(**doc)
    if not include_drafts and post.status != BlogPostStatus.PUBLISHED:
        raise BlogPostNotFound(slug)
    return post


async def list_posts(
    *, pagination: PaginationParams, status_filter: Optional[BlogPostStatus] = None
) -> tuple[list[ContentBlogPostDocument], int]:
    """Returns `(page_of_posts, total_matching_count)`.

    The public route always calls this with `status_filter=BlogPostStatus.PUBLISHED`
    (never anything else - drafts must never appear in the public list) and
    relies on the sort below matching the `content_blog_posts_status_published_at`
    compound index (`app/core/database.py::ensure_indexes`) for an efficient
    query at any collection size (see `content_blog_post.py`'s module
    docstring for why this collection, unlike its siblings, is expected to
    grow unboundedly). The admin route may pass `status_filter=None` (every
    post regardless of status) or a specific status.

    Sorted by `published_at` descending - draft posts (`published_at is None`)
    naturally sort last under a descending sort when `status_filter=None`, which
    is an acceptable ordering for an admin "all posts" view; the public,
    published-only view never has a `None` `published_at` to worry about
    since every published post has one stamped (see module docstring).
    """
    query: dict = {}
    if status_filter is not None:
        query["status"] = status_filter.value

    total = await db.content_blog_posts.count_documents(query)
    cursor = (
        db.content_blog_posts.find(query)
        .sort("published_at", -1)
        .skip(pagination.offset)
        .limit(pagination.page_size)
    )
    posts = [ContentBlogPostDocument(**doc) async for doc in cursor]
    return posts, total


async def create_post(body: ContentBlogPostCreate) -> ContentBlogPostDocument:
    await _validate_category(body.category)

    now = datetime.utcnow()
    insert_doc = body.model_dump()
    insert_doc["tags"] = await _normalize_tags(body.tags)
    # Never trust caller-supplied created_at/updated_at (both are real fields
    # on ContentBlogPostCreate, so extra="forbid" doesn't block a client from
    # setting them) - always stamp fresh server-side, mirroring
    # tool_metadata_service.create_tool_metadata's identical override.
    insert_doc["created_at"] = now
    insert_doc["updated_at"] = now
    # A post can be born already-published (status="published" at create
    # time) - stamp published_at once, at insert, in that case. See module
    # docstring's published_at section.
    insert_doc["published_at"] = now if body.status == BlogPostStatus.PUBLISHED else None

    try:
        insert_result = await db.content_blog_posts.insert_one(insert_doc)
    except DuplicateKeyError as exc:
        logger.warning("Rejected duplicate content_blog_posts slug on create: %s", body.slug)
        raise BlogPostSlugConflict(body.slug) from exc

    doc = await db.content_blog_posts.find_one({"_id": insert_result.inserted_id})
    logger.info("Created content_blog_posts document id=%s slug=%s", insert_result.inserted_id, body.slug)
    return ContentBlogPostDocument(**doc)


async def update_post(slug: str, body: ContentBlogPostUpdate) -> ContentBlogPostDocument:
    # include_drafts=True: an admin editing a post must be able to load it
    # regardless of its current status.
    existing = await get_by_slug(slug, include_drafts=True)  # raises BlogPostNotFound

    if body.category is not None:
        await _validate_category(body.category)

    update_doc: dict = {"updated_at": datetime.utcnow()}
    if body.title is not None:
        update_doc["title"] = body.title
    if body.category is not None:
        update_doc["category"] = body.category
    if body.tags is not None:
        update_doc["tags"] = await _normalize_tags(body.tags)
    if body.excerpt is not None:
        update_doc["excerpt"] = body.excerpt
    if body.body is not None:
        update_doc["body"] = body.body
    if body.cover_image_url is not None:
        update_doc["cover_image_url"] = body.cover_image_url
    if body.ad_slot is not None:
        update_doc["ad_slot"] = body.ad_slot.model_dump()
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
            body.status == BlogPostStatus.PUBLISHED
            and existing.status != BlogPostStatus.PUBLISHED
            and existing.published_at is None
        ):
            update_doc["published_at"] = datetime.utcnow()

    await db.content_blog_posts.update_one({"_id": existing.id}, {"$set": update_doc})

    doc = await db.content_blog_posts.find_one({"_id": existing.id})
    logger.info("Updated content_blog_posts document slug=%s", slug)
    return ContentBlogPostDocument(**doc)


async def delete_post(slug: str) -> None:
    existing = await get_by_slug(slug, include_drafts=True)  # raises BlogPostNotFound
    await db.content_blog_posts.delete_one({"_id": existing.id})
    logger.info("Deleted content_blog_posts document slug=%s", slug)
