"""Schema + service + pagination + HTTP tests for the Blog/News CMS
(`content` module, direct extension of the Tools Metadata CMS - see
`app/schemas/content_blog_post.py`'s module docstring for full feature
background).

Mirrors `tests/test_content_tool_metadata.py`'s harness pattern exactly
(same reasons documented there: `app.main` isn't importable in this
checkout, so this file builds its own tiny `FastAPI()` app mounting only
`content.public_router`/`content.router`, replicates `app/main.py`'s
`protected_dependency = [Depends(verify_api_key), Depends(get_rate_limit)]`
locally, and mints a valid admin session directly via
`create_admin_access_token` rather than a real HTTP login round trip). Also
registers `app.state.limiter`/the `RateLimitExceeded` exception handler
(mirroring `tests/test_auth.py::_build_test_app`) because the two public
blog-post routes carry their own `@limiter.limit(...)` decorators.

Real local Mongo (`mongodb://localhost:27017`, db `pdfconverterai`), same as
every other test file in this suite. `content_blog_posts` has a unique index
on `slug` (`content_blog_posts_slug_unique`) created by
`app.core.database.ensure_indexes()` - this file calls it once per session
before any test runs, same as `test_content_tool_metadata.py`'s
`_ensure_content_indexes` fixture, so the slug-conflict test below exercises
the real `DuplicateKeyError` translation path.

`content_categories` rows with `content_type="blog"` are inserted directly
via `_insert_blog_category_direct` for test setup rather than relying on any
real seeded rows being present in this checkout, so these tests are
self-contained.

Covers, roughly in this order:
  1. `app/schemas/content_blog_post.py` - slug format validator,
     `cover_image_url` http(s)-only validator on both `ContentBlogPostBase`
     and `ContentBlogPostUpdate`, `extra="forbid"` rejecting `published_at`/
     `slug` on Update, `status` defaulting to draft on Create.
  2. `app/shared/pagination.py` - `PaginationParams.offset` math,
     `paginated_envelope`'s ceiling-division `total_pages` math (including
     `total=0`), and the `page`/`page_size` Query bounds exercised through
     the real HTTP list route (FastAPI's own `Query(ge=..., le=...)`
     validation only fires through the dependency-injection/HTTP path, not
     a direct Python call).
  3. `app/services/content/blog_posts_service.py` - every `published_at`
     stamping branch, category validation against the `content_type="blog"`
     slice (rejecting a `tool_metadata`-only category and vice versa), tag
     normalization/dedup, slug-conflict, and draft invisibility.
  4. `app/routers/content.py`'s blog-post routes - public routes never leak
     drafts, the public list route never accepts a client-supplied status
     filter, all 5 admin routes require `require_admin` (parametrized,
     mirrors `test_content_tool_metadata.py`'s
     `test_admin_route_requires_api_key_layer_wrong_category_403`-style
     tests), and the two new rate limits actually reject an over-limit
     request.
"""
import logging
from datetime import datetime

import pytest
import pytest_asyncio
from bson import ObjectId
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.database import db, ensure_indexes
from app.core.rate_limiter import limiter
from app.core.security import verify_api_key
from app.routers import content as content_router
from app.schemas.content_blog_post import (
    BlogPostStatus,
    ContentBlogPostCreate,
    ContentBlogPostUpdate,
)
from app.services.auth.token_service import create_admin_access_token
from app.services.content.blog_posts_service import (
    BlogPostNotFound,
    BlogPostSlugConflict,
    InvalidCategory,
    create_post,
    delete_post,
    get_by_slug,
    list_posts,
    update_post,
)
from app.shared.pagination import PaginationParams, paginated_envelope

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ADMIN_EMAIL = "seed-test-admin@pdfconverterai.com"
_ADMIN_COOKIE_NAME = "admin_session"

logger = logging.getLogger(__name__)


# --- test app scaffolding (identical to test_content_tool_metadata.py, plus
# app.state.limiter/RateLimitExceeded wiring for the two rate-limited public
# blog routes - mirrors tests/test_auth.py::_build_test_app) --------------


async def _get_rate_limit(key_info: dict = Depends(verify_api_key)):
    key_data = key_info["key_data"]
    if key_data["type"] == "internal":
        return limiter.limit("100/minute")
    return limiter.limit(f"{key_data['rate_limit_per_day']}/day")


_protected_dependency = [Depends(verify_api_key), Depends(_get_rate_limit)]


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.include_router(content_router.public_router, prefix="/v1")
    app.include_router(content_router.router, prefix="/v1", dependencies=_protected_dependency)

    @app.exception_handler(StarletteHTTPException)
    async def http_exception_handler(request: Request, exc: StarletteHTTPException):
        detail = exc.detail
        if isinstance(detail, dict) and "success" in detail:
            return JSONResponse(status_code=exc.status_code, content=detail)
        return JSONResponse(
            status_code=exc.status_code,
            content={"success": False, "message": str(detail), "error": {"code": "HTTP_ERROR"}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(
            status_code=422,
            content={"success": False, "message": "Invalid request", "error": {"code": "VALIDATION_ERROR"}},
        )

    return app


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _ensure_content_indexes():
    await ensure_indexes()
    yield


@pytest_asyncio.fixture
async def client():
    app = _build_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    limiter.reset()
    yield
    limiter.reset()


@pytest_asyncio.fixture
async def api_key():
    """A fresh API key granted the `content` category only."""
    key_value = f"content-blog-test-key-{ObjectId()}"
    doc = {
        "key": key_value,
        "status": "active",
        "usage_count": 0,
        "rate_limit_per_day": 100_000,
        "categories": ["content"],
        "type": "external",
        "created_at": datetime.utcnow(),
    }
    result = await db.api_keys.insert_one(doc)
    yield key_value
    await db.api_keys.delete_one({"_id": result.inserted_id})


@pytest_asyncio.fixture
async def wrong_category_api_key():
    """A valid, active API key NOT granted the `content` category."""
    key_value = f"wrong-category-blog-test-key-{ObjectId()}"
    doc = {
        "key": key_value,
        "status": "active",
        "usage_count": 0,
        "rate_limit_per_day": 100_000,
        "categories": ["pdf"],
        "type": "external",
        "created_at": datetime.utcnow(),
    }
    result = await db.api_keys.insert_one(doc)
    yield key_value
    await db.api_keys.delete_one({"_id": result.inserted_id})


@pytest.fixture
def admin_cookie() -> dict:
    token = create_admin_access_token(_ADMIN_EMAIL)
    return {_ADMIN_COOKIE_NAME: token}


@pytest_asyncio.fixture
async def created_category_ids():
    """Tracks every directly-inserted `content_categories._id` (always
    `content_type="blog"` in this file) and deletes exactly those documents
    after the test."""
    ids: list[ObjectId] = []
    yield ids
    if ids:
        await db.content_categories.delete_many({"_id": {"$in": ids}})


@pytest_asyncio.fixture
async def created_blog_slugs():
    """Tracks every `content_blog_posts.slug` a test creates (via the API or
    direct service call) and deletes exactly those documents after the
    test."""
    slugs: list[str] = []
    yield slugs
    if slugs:
        await db.content_blog_posts.delete_many({"slug": {"$in": slugs}})


@pytest_asyncio.fixture
async def created_tag_slugs():
    """Tracks every `tags.slug` this file's tests cause to be created and
    deletes exactly those documents after the test."""
    slugs: list[str] = []
    yield slugs
    if slugs:
        await db.tags.delete_many({"slug": {"$in": slugs}})


def _auth_headers(api_key_value: str) -> dict:
    return {"X-API-Key": api_key_value}


async def _insert_blog_category_direct(label: str, slug: str, order: int = 0) -> ObjectId:
    now = datetime.utcnow()
    doc = {
        "label": label,
        "slug": slug,
        "content_type": "blog",
        "color_token": None,
        "order": order,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.content_categories.insert_one(doc)
    return result.inserted_id


async def _insert_tool_metadata_category_direct(label: str, slug: str, order: int = 0) -> ObjectId:
    """Used only for the "wrong content_type" category-rejection tests -
    a category slug that genuinely exists, but under `content_type=
    'tool_metadata'` instead of `'blog'`."""
    now = datetime.utcnow()
    doc = {
        "label": label,
        "slug": slug,
        "content_type": "tool_metadata",
        "color_token": slug,
        "order": order,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.content_categories.insert_one(doc)
    return result.inserted_id


def _blog_payload(slug: str, category: str, **overrides) -> dict:
    payload = {
        "slug": slug,
        "title": overrides.get("title", "Test Post"),
        "category": category,
        "excerpt": overrides.get("excerpt", "A short teaser for the post."),
        "body": overrides.get("body", "The full body of the test post."),
        "tags": overrides.get("tags", []),
    }
    if "cover_image_url" in overrides:
        payload["cover_image_url"] = overrides["cover_image_url"]
    if "status" in overrides:
        payload["status"] = overrides["status"]
    if "ad_slot" in overrides:
        payload["ad_slot"] = overrides["ad_slot"]
    return payload


def _create_model(slug: str, category: str, **overrides) -> ContentBlogPostCreate:
    return ContentBlogPostCreate(**_blog_payload(slug, category, **overrides))


# ===========================================================================
# 1. Schema tests: app/schemas/content_blog_post.py
# ===========================================================================


# --- slug format validator -------------------------------------------------


def test_slug_valid_lowercase_hyphenated_accepted():
    model = _create_model("a-valid-slug-123", "irrelevant-category")
    assert model.slug == "a-valid-slug-123"


@pytest.mark.parametrize(
    "bad_slug",
    [
        "Has-Uppercase",
        "has_underscore",
        "-leading-hyphen",
        "trailing-hyphen-",
        "double--hyphen",
        "has space",
        "",
    ],
)
def test_slug_invalid_formats_rejected(bad_slug):
    with pytest.raises(ValidationError):
        _create_model(bad_slug, "irrelevant-category")


# --- cover_image_url http(s)-only validator ---------------------------------


def test_cover_image_url_https_accepted_on_create():
    model = _create_model("cover-https-test-1", "cat", cover_image_url="https://cdn.example.com/a.png")
    assert model.cover_image_url == "https://cdn.example.com/a.png"


def test_cover_image_url_http_accepted_on_create():
    model = _create_model("cover-http-test-1", "cat", cover_image_url="http://cdn.example.com/a.png")
    assert model.cover_image_url == "http://cdn.example.com/a.png"


def test_cover_image_url_none_allowed_on_create():
    model = _create_model("cover-none-test-1", "cat")
    assert model.cover_image_url is None


@pytest.mark.parametrize(
    "bad_url",
    [
        "javascript:alert(1)",
        "data:image/png;base64,aaaa",
        "ftp://example.com/a.png",
        "//example.com/a.png",
        "example.com/a.png",
    ],
)
def test_cover_image_url_non_http_schemes_rejected_on_create(bad_url):
    with pytest.raises(ValidationError):
        _create_model("cover-bad-scheme-test-1", "cat", cover_image_url=bad_url)


def test_cover_image_url_https_accepted_on_update():
    model = ContentBlogPostUpdate(cover_image_url="https://cdn.example.com/a.png")
    assert model.cover_image_url == "https://cdn.example.com/a.png"


@pytest.mark.parametrize("bad_url", ["javascript:alert(1)", "ftp://example.com/a.png"])
def test_cover_image_url_non_http_schemes_rejected_on_update(bad_url):
    with pytest.raises(ValidationError):
        ContentBlogPostUpdate(cover_image_url=bad_url)


# --- extra="forbid" rejects published_at/slug on Update ---------------------


def test_update_schema_rejects_published_at_extra_field():
    with pytest.raises(ValidationError):
        ContentBlogPostUpdate(title="New title", published_at="2020-01-01T00:00:00")


def test_update_schema_rejects_slug_extra_field():
    with pytest.raises(ValidationError):
        ContentBlogPostUpdate(title="New title", slug="attempted-new-slug")


def test_update_schema_accepts_known_fields_only():
    model = ContentBlogPostUpdate(title="New title", status=BlogPostStatus.PUBLISHED)
    assert model.title == "New title"
    assert model.status == BlogPostStatus.PUBLISHED


# --- status defaults to draft on Create -------------------------------------


def test_create_status_defaults_to_draft_when_omitted():
    model = _create_model("status-default-test-1", "cat")
    assert model.status == BlogPostStatus.DRAFT


def test_create_status_explicit_published_accepted():
    model = _create_model("status-explicit-published-test-1", "cat", status="published")
    assert model.status == BlogPostStatus.PUBLISHED


def test_create_schema_rejects_published_at_extra_field():
    """ContentBlogPostCreate inherits extra="forbid" from ContentBlogPostBase
    and deliberately does not declare `published_at` - a client-supplied
    value must be rejected outright, not silently accepted/ignored."""
    payload = _blog_payload("create-rejects-published-at-test-1", "cat")
    payload["published_at"] = "2020-01-01T00:00:00"
    with pytest.raises(ValidationError):
        ContentBlogPostCreate(**payload)


# ===========================================================================
# 2. Pagination tests: app/shared/pagination.py
# ===========================================================================


def test_pagination_params_offset_first_page_is_zero():
    assert PaginationParams(page=1, page_size=10).offset == 0


def test_pagination_params_offset_later_page():
    assert PaginationParams(page=3, page_size=10).offset == 20


def test_pagination_params_offset_non_default_page_size():
    assert PaginationParams(page=4, page_size=5).offset == 15


def test_paginated_envelope_total_zero_gives_zero_total_pages():
    result = paginated_envelope([], 0, PaginationParams(page=1, page_size=10))
    assert result["total"] == 0
    assert result["total_pages"] == 0
    assert result["items"] == []


def test_paginated_envelope_exact_multiple_of_page_size():
    result = paginated_envelope([], 20, PaginationParams(page=1, page_size=10))
    assert result["total_pages"] == 2


def test_paginated_envelope_ceiling_division_remainder():
    result = paginated_envelope([], 21, PaginationParams(page=1, page_size=10))
    assert result["total_pages"] == 3


def test_paginated_envelope_single_item_single_page():
    result = paginated_envelope([{"x": 1}], 1, PaginationParams(page=1, page_size=10))
    assert result["total_pages"] == 1
    assert result["page"] == 1
    assert result["page_size"] == 10


async def test_pagination_defaults_via_http_list_route(client):
    resp = await client.get("/v1/content/blog-posts")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["page"] == 1
    assert data["page_size"] == 10


async def test_pagination_page_lower_bound_rejected_422(client):
    resp = await client.get("/v1/content/blog-posts", params={"page": 0})
    assert resp.status_code == 422, resp.text


async def test_pagination_page_size_lower_bound_rejected_422(client):
    resp = await client.get("/v1/content/blog-posts", params={"page_size": 0})
    assert resp.status_code == 422, resp.text


async def test_pagination_page_size_upper_bound_rejected_422(client):
    resp = await client.get("/v1/content/blog-posts", params={"page_size": 51})
    assert resp.status_code == 422, resp.text


async def test_pagination_page_size_at_upper_bound_accepted(client):
    resp = await client.get("/v1/content/blog-posts", params={"page_size": 50})
    assert resp.status_code == 200, resp.text


# ===========================================================================
# 3. Service layer tests: app/services/content/blog_posts_service.py
# ===========================================================================


# --- published_at stamping: create_post -------------------------------------


async def test_create_post_born_published_stamps_published_at(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-born-published-test-1")
    created_category_ids.append(category_id)

    slug = "born-published-test-1"
    before = datetime.utcnow()
    post = await create_post(_create_model(slug, "news-born-published-test-1", status="published"))
    after = datetime.utcnow()
    created_blog_slugs.append(slug)

    assert post.status == BlogPostStatus.PUBLISHED
    assert post.published_at is not None
    assert before <= post.published_at <= after


async def test_create_post_draft_leaves_published_at_none(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-draft-create-test-1")
    created_category_ids.append(category_id)

    slug = "draft-create-test-1"
    post = await create_post(_create_model(slug, "news-draft-create-test-1"))
    created_blog_slugs.append(slug)

    assert post.status == BlogPostStatus.DRAFT
    assert post.published_at is None


# --- published_at stamping: update_post -------------------------------------


async def test_update_post_first_publish_stamps_published_at(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-first-publish-test-1")
    created_category_ids.append(category_id)

    slug = "first-publish-test-1"
    await create_post(_create_model(slug, "news-first-publish-test-1"))
    created_blog_slugs.append(slug)

    before = datetime.utcnow()
    updated = await update_post(slug, ContentBlogPostUpdate(status=BlogPostStatus.PUBLISHED))
    after = datetime.utcnow()

    assert updated.status == BlogPostStatus.PUBLISHED
    assert updated.published_at is not None
    assert before <= updated.published_at <= after


async def test_update_post_already_published_edit_does_not_restamp(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-no-restamp-test-1")
    created_category_ids.append(category_id)

    slug = "no-restamp-test-1"
    await create_post(_create_model(slug, "news-no-restamp-test-1", status="published"))
    created_blog_slugs.append(slug)

    original = await get_by_slug(slug, include_drafts=True)
    assert original.published_at is not None

    # A plain content edit that also re-asserts status="published" (an
    # already-published post edited again) must not bump published_at.
    updated = await update_post(
        slug, ContentBlogPostUpdate(title="Edited title", status=BlogPostStatus.PUBLISHED)
    )
    assert updated.published_at == original.published_at
    assert updated.title == "Edited title"


async def test_update_post_unpublish_does_not_clear_published_at(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-unpublish-test-1")
    created_category_ids.append(category_id)

    slug = "unpublish-test-1"
    await create_post(_create_model(slug, "news-unpublish-test-1", status="published"))
    created_blog_slugs.append(slug)

    original = await get_by_slug(slug, include_drafts=True)
    assert original.published_at is not None

    updated = await update_post(slug, ContentBlogPostUpdate(status=BlogPostStatus.DRAFT))
    assert updated.status == BlogPostStatus.DRAFT
    assert updated.published_at == original.published_at  # untouched, not cleared


async def test_update_post_republish_after_past_unpublish_does_not_restamp(
    created_category_ids, created_blog_slugs
):
    category_id = await _insert_blog_category_direct("News", "news-republish-test-1")
    created_category_ids.append(category_id)

    slug = "republish-test-1"
    await create_post(_create_model(slug, "news-republish-test-1", status="published"))
    created_blog_slugs.append(slug)

    first_published_at = (await get_by_slug(slug, include_drafts=True)).published_at

    await update_post(slug, ContentBlogPostUpdate(status=BlogPostStatus.DRAFT))
    republished = await update_post(slug, ContentBlogPostUpdate(status=BlogPostStatus.PUBLISHED))

    assert republished.status == BlogPostStatus.PUBLISHED
    assert republished.published_at == first_published_at  # never bumped a second time


# --- category validation against content_type="blog" -----------------------


async def test_create_post_rejects_unknown_category(created_blog_slugs):
    slug = "unknown-category-test-1"
    with pytest.raises(InvalidCategory):
        await create_post(_create_model(slug, "not-a-real-category-slug-1"))
    created_blog_slugs.append(slug)  # no-op if rejected, safe either way
    doc = await db.content_blog_posts.find_one({"slug": slug})
    assert doc is None


async def test_create_post_rejects_tool_metadata_only_category(created_category_ids, created_blog_slugs):
    """A category slug that exists but is `content_type="tool_metadata"`
    (not `blog`) must still be rejected - proves the validation filters by
    content_type, not just slug existence."""
    category_id = await _insert_tool_metadata_category_direct("PDF", "pdf-tool-only-category-test-1")
    created_category_ids.append(category_id)

    slug = "wrong-content-type-for-blog-test-1"
    with pytest.raises(InvalidCategory):
        await create_post(_create_model(slug, "pdf-tool-only-category-test-1"))
    created_blog_slugs.append(slug)
    doc = await db.content_blog_posts.find_one({"slug": slug})
    assert doc is None


async def test_update_post_rejects_invalid_category(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-update-invalidcat-test-1")
    created_category_ids.append(category_id)

    slug = "update-invalid-category-test-1"
    await create_post(_create_model(slug, "news-update-invalidcat-test-1"))
    created_blog_slugs.append(slug)

    with pytest.raises(InvalidCategory):
        await update_post(slug, ContentBlogPostUpdate(category="not-a-real-category-slug-1"))

    doc = await db.content_blog_posts.find_one({"slug": slug})
    assert doc["category"] == "news-update-invalidcat-test-1"  # unchanged by rejected update


# --- tag normalization/dedup -------------------------------------------------


async def test_create_post_normalizes_and_dedups_tags(created_category_ids, created_blog_slugs, created_tag_slugs):
    category_id = await _insert_blog_category_direct("News", "news-tags-test-1")
    created_category_ids.append(category_id)

    slug = "tags-normalize-test-1"
    post = await create_post(
        _create_model(slug, "news-tags-test-1", tags=["News", "news", "  Breaking News "])
    )
    created_blog_slugs.append(slug)
    created_tag_slugs.extend(["news", "breaking-news"])

    assert set(post.tags) == {"news", "breaking-news"}
    assert len(post.tags) == 2  # deduped, not ["news", "news", "breaking-news"]


# --- slug uniqueness ---------------------------------------------------------


async def test_create_post_duplicate_slug_raises_conflict(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-dup-slug-test-1")
    created_category_ids.append(category_id)

    slug = "dup-slug-service-test-1"
    await create_post(_create_model(slug, "news-dup-slug-test-1", title="First"))
    created_blog_slugs.append(slug)

    with pytest.raises(BlogPostSlugConflict):
        await create_post(_create_model(slug, "news-dup-slug-test-1", title="Second"))

    doc = await db.content_blog_posts.find_one({"slug": slug})
    assert doc["title"] == "First"
    assert await db.content_blog_posts.count_documents({"slug": slug}) == 1


# --- draft invisibility (get_by_slug) ---------------------------------------


async def test_get_by_slug_draft_raises_not_found_when_public(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-draft-invisible-test-1")
    created_category_ids.append(category_id)

    slug = "draft-invisible-test-1"
    await create_post(_create_model(slug, "news-draft-invisible-test-1"))  # draft by default
    created_blog_slugs.append(slug)

    with pytest.raises(BlogPostNotFound):
        await get_by_slug(slug, include_drafts=False)


async def test_get_by_slug_draft_visible_to_admin(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-draft-visible-admin-test-1")
    created_category_ids.append(category_id)

    slug = "draft-visible-admin-test-1"
    await create_post(_create_model(slug, "news-draft-visible-admin-test-1"))
    created_blog_slugs.append(slug)

    post = await get_by_slug(slug, include_drafts=True)
    assert post.slug == slug
    assert post.status == BlogPostStatus.DRAFT


async def test_get_by_slug_truly_missing_raises_not_found():
    with pytest.raises(BlogPostNotFound):
        await get_by_slug("does-not-exist-service-test-1", include_drafts=False)


async def test_delete_post_removes_row_and_raises_on_second_delete(created_category_ids):
    category_id = await _insert_blog_category_direct("News", "news-delete-test-1")
    created_category_ids.append(category_id)

    slug = "delete-service-test-1"
    await create_post(_create_model(slug, "news-delete-test-1"))

    await delete_post(slug)
    doc = await db.content_blog_posts.find_one({"slug": slug})
    assert doc is None

    with pytest.raises(BlogPostNotFound):
        await delete_post(slug)


async def test_list_posts_filters_by_status(created_category_ids, created_blog_slugs):
    category_id = await _insert_blog_category_direct("News", "news-list-filter-test-1")
    created_category_ids.append(category_id)

    published_slug = "list-filter-published-test-1"
    draft_slug = "list-filter-draft-test-1"
    await create_post(_create_model(published_slug, "news-list-filter-test-1", status="published"))
    await create_post(_create_model(draft_slug, "news-list-filter-test-1"))
    created_blog_slugs.extend([published_slug, draft_slug])

    posts, total = await list_posts(
        pagination=PaginationParams(page=1, page_size=50), status_filter=BlogPostStatus.PUBLISHED
    )
    slugs = {p.slug for p in posts}
    assert published_slug in slugs
    assert draft_slug not in slugs
    assert total >= 1


# ===========================================================================
# 4. HTTP tests: app/routers/content.py's blog-post routes
# ===========================================================================


# --- public routes never leak drafts ----------------------------------------


async def test_public_get_blog_post_published_returns_200(
    client, api_key, admin_cookie, created_category_ids, created_blog_slugs
):
    category_id = await _insert_blog_category_direct("News", "news-public-get-test-1")
    created_category_ids.append(category_id)

    slug = "public-get-published-test-1"
    create_resp = await client.post(
        "/v1/content/blog-posts",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload(slug, "news-public-get-test-1", status="published", title="Big News"),
    )
    assert create_resp.status_code == 200, create_resp.text
    created_blog_slugs.append(slug)

    # No headers/cookies at all - confirms this route needs no auth.
    resp = await client.get(f"/v1/content/blog-posts/{slug}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["slug"] == slug
    assert data["title"] == "Big News"
    assert data["status"] == "published"


async def test_public_get_blog_post_draft_returns_404_not_leaked(
    client, api_key, admin_cookie, created_category_ids, created_blog_slugs
):
    category_id = await _insert_blog_category_direct("News", "news-public-draft-test-1")
    created_category_ids.append(category_id)

    slug = "public-draft-hidden-test-1"
    create_resp = await client.post(
        "/v1/content/blog-posts",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload(slug, "news-public-draft-test-1"),  # draft by default
    )
    assert create_resp.status_code == 200, create_resp.text
    created_blog_slugs.append(slug)

    resp = await client.get(f"/v1/content/blog-posts/{slug}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "BLOG_POST_NOT_FOUND"


async def test_public_get_blog_post_missing_slug_returns_404(client):
    resp = await client.get("/v1/content/blog-posts/does-not-exist-public-test-1")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "BLOG_POST_NOT_FOUND"


async def test_public_list_never_includes_draft_posts(
    client, api_key, admin_cookie, created_category_ids, created_blog_slugs
):
    category_id = await _insert_blog_category_direct("News", "news-public-list-test-1")
    created_category_ids.append(category_id)

    published_slug = "public-list-published-test-1"
    draft_slug = "public-list-draft-test-1"
    for slug, status in [(published_slug, "published"), (draft_slug, "draft")]:
        create_resp = await client.post(
            "/v1/content/blog-posts",
            headers=_auth_headers(api_key),
            cookies=admin_cookie,
            json=_blog_payload(slug, "news-public-list-test-1", status=status),
        )
        assert create_resp.status_code == 200, create_resp.text
        created_blog_slugs.append(slug)

    resp = await client.get("/v1/content/blog-posts", params={"page_size": 50})
    assert resp.status_code == 200, resp.text
    slugs = {item["slug"] for item in resp.json()["data"]["items"]}
    assert published_slug in slugs
    assert draft_slug not in slugs


async def test_public_list_ignores_client_supplied_status_filter(
    client, api_key, admin_cookie, created_category_ids, created_blog_slugs
):
    """The public list route (`get_public_blog_posts`) declares no `status`
    query parameter at all - it is hardcoded server-side to
    `status_filter=BlogPostStatus.PUBLISHED`. Passing `?status=draft` must
    not surface any draft post (FastAPI silently ignores undeclared query
    params rather than rejecting them, so this must be asserted via
    behavior, not a 422)."""
    category_id = await _insert_blog_category_direct("News", "news-status-filter-ignored-test-1")
    created_category_ids.append(category_id)

    draft_slug = "status-filter-ignored-draft-test-1"
    create_resp = await client.post(
        "/v1/content/blog-posts",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload(draft_slug, "news-status-filter-ignored-test-1"),
    )
    assert create_resp.status_code == 200, create_resp.text
    created_blog_slugs.append(draft_slug)

    resp = await client.get(
        "/v1/content/blog-posts", params={"status": "draft", "page_size": 50}
    )
    assert resp.status_code == 200, resp.text
    slugs = {item["slug"] for item in resp.json()["data"]["items"]}
    assert draft_slug not in slugs


async def test_blog_post_public_routes_are_registered_on_public_router_only():
    """Confirms the public routes are actually declared on `public_router`
    (never gated by `protected_dependency`/`require_admin`) with only the
    expected HTTP methods, rather than merely happening to work
    unauthenticated by accident."""
    public_paths = {route.path for route in content_router.public_router.routes}
    assert "/content/blog-posts" in public_paths
    assert "/content/blog-posts/{slug}" in public_paths

    public_list_route = next(
        r for r in content_router.public_router.routes if r.path == "/content/blog-posts"
    )
    assert public_list_route.methods == {"GET"}
    public_detail_route = next(
        r for r in content_router.public_router.routes if r.path == "/content/blog-posts/{slug}"
    )
    assert public_detail_route.methods == {"GET"}


# --- admin CRUD happy path ---------------------------------------------------


async def test_admin_create_list_get_update_delete_round_trip(
    client, api_key, admin_cookie, created_category_ids, created_blog_slugs
):
    category_id = await _insert_blog_category_direct("News", "news-admin-crud-test-1")
    created_category_ids.append(category_id)

    slug = "admin-crud-round-trip-test-1"
    create_resp = await client.post(
        "/v1/content/blog-posts",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload(slug, "news-admin-crud-test-1", title="Original Title"),
    )
    assert create_resp.status_code == 200, create_resp.text
    created_blog_slugs.append(slug)

    list_resp = await client.get(
        "/v1/content/admin/blog-posts", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert list_resp.status_code == 200, list_resp.text
    assert slug in {item["slug"] for item in list_resp.json()["data"]["items"]}

    get_resp = await client.get(
        f"/v1/content/admin/blog-posts/{slug}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["data"]["title"] == "Original Title"

    update_resp = await client.put(
        f"/v1/content/blog-posts/{slug}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"title": "Updated Title"},
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["data"]["title"] == "Updated Title"

    first_delete = await client.delete(
        f"/v1/content/blog-posts/{slug}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert first_delete.status_code == 200, first_delete.text

    second_delete = await client.delete(
        f"/v1/content/blog-posts/{slug}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert second_delete.status_code == 404, second_delete.text
    assert second_delete.json()["error"]["code"] == "BLOG_POST_NOT_FOUND"


async def test_admin_create_duplicate_slug_returns_409(
    client, api_key, admin_cookie, created_category_ids, created_blog_slugs
):
    category_id = await _insert_blog_category_direct("News", "news-admin-dup-test-1")
    created_category_ids.append(category_id)

    slug = "admin-dup-slug-http-test-1"
    first = await client.post(
        "/v1/content/blog-posts",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload(slug, "news-admin-dup-test-1"),
    )
    assert first.status_code == 200, first.text
    created_blog_slugs.append(slug)

    second = await client.post(
        "/v1/content/blog-posts",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload(slug, "news-admin-dup-test-1"),
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "BLOG_POST_SLUG_CONFLICT"


async def test_admin_create_invalid_category_returns_400(client, api_key, admin_cookie, created_blog_slugs):
    slug = "admin-invalid-category-http-test-1"
    resp = await client.post(
        "/v1/content/blog-posts",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload(slug, "not-a-real-category-slug-1"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_CATEGORY"
    created_blog_slugs.append(slug)


async def test_admin_update_unknown_slug_returns_404(client, api_key, admin_cookie):
    resp = await client.put(
        "/v1/content/blog-posts/does-not-exist-update-http-test-1",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"title": "Doesn't matter"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "BLOG_POST_NOT_FOUND"


# --- every admin write route (+ list/get) rejects bad/missing auth ---------


def _valid_create_body() -> dict:
    return _blog_payload("auth-gate-blog-test-slug-1", "auth-gate-irrelevant-category")


def _valid_update_body() -> dict:
    return {"title": "Doesn't matter"}


_ADMIN_BLOG_ROUTES = [
    ("post", "/v1/content/blog-posts", _valid_create_body()),
    ("get", "/v1/content/admin/blog-posts", None),
    ("get", "/v1/content/admin/blog-posts/auth-gate-blog-test-slug-1", None),
    ("put", "/v1/content/blog-posts/auth-gate-blog-test-slug-1", _valid_update_body()),
    ("delete", "/v1/content/blog-posts/auth-gate-blog-test-slug-1", None),
]


@pytest.mark.parametrize("method,path,json_body", _ADMIN_BLOG_ROUTES)
async def test_admin_blog_route_requires_api_key_layer_missing_header_422(
    client, admin_cookie, method, path, json_body
):
    resp = await client.request(method.upper(), path, cookies=admin_cookie, json=json_body)
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_BLOG_ROUTES)
async def test_admin_blog_route_requires_api_key_layer_invalid_key_403(
    client, admin_cookie, method, path, json_body
):
    resp = await client.request(
        method.upper(), path, headers=_auth_headers("not-a-real-key"), cookies=admin_cookie, json=json_body
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_BLOG_ROUTES)
async def test_admin_blog_route_requires_api_key_layer_wrong_category_403(
    client, admin_cookie, wrong_category_api_key, method, path, json_body
):
    resp = await client.request(
        method.upper(), path, headers=_auth_headers(wrong_category_api_key), cookies=admin_cookie, json=json_body
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_BLOG_ROUTES)
async def test_admin_blog_route_valid_api_key_but_no_cookie_401(client, api_key, method, path, json_body):
    resp = await client.request(method.upper(), path, headers=_auth_headers(api_key), json=json_body)
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_BLOG_ROUTES)
async def test_admin_blog_route_valid_api_key_but_invalid_cookie_401(client, api_key, method, path, json_body):
    resp = await client.request(
        method.upper(),
        path,
        headers=_auth_headers(api_key),
        cookies={_ADMIN_COOKIE_NAME: "garbage-token"},
        json=json_body,
    )
    assert resp.status_code == 401, resp.text


# --- rate limiting on the two public blog-post routes -----------------------


async def test_public_blog_list_rate_limit_returns_429_after_threshold(client):
    """Exercises `get_public_blog_posts`'s `@limiter.limit("30/minute")` end
    to end. The literal `30` below must stay in sync with that decorator's
    value."""
    _RATE_LIMIT = 30
    for _ in range(_RATE_LIMIT):
        resp = await client.get("/v1/content/blog-posts")
        assert resp.status_code == 200, resp.text

    limited_resp = await client.get("/v1/content/blog-posts")
    assert limited_resp.status_code == 429, limited_resp.text


async def test_public_blog_single_rate_limit_returns_429_after_threshold(client):
    """Exercises `get_public_blog_post`'s `@limiter.limit("60/minute")` end
    to end. The literal `60` below must stay in sync with that decorator's
    value. Uses a guaranteed-missing slug (404s are still counted against
    the rate limit budget, same as a real hit) to avoid any DB setup cost
    across 61 requests."""
    _RATE_LIMIT = 60
    for _ in range(_RATE_LIMIT):
        resp = await client.get("/v1/content/blog-posts/rate-limit-probe-slug-1")
        assert resp.status_code == 404, resp.text

    limited_resp = await client.get("/v1/content/blog-posts/rate-limit-probe-slug-1")
    assert limited_resp.status_code == 429, limited_resp.text
