"""Schema + service + HTTP tests for the Dynamic Pages builder (`content`
module, ADR-022: Dynamic CMS Pages - content-block model and route-collision
handling - see `app/schemas/content_page.py`'s module docstring for full
feature background).

Mirrors `tests/test_content_tool_metadata.py`'s harness pattern exactly
(same reasons documented there: `app.main` isn't importable in this
checkout, so this file builds its own tiny `FastAPI()` app mounting only
`content.public_router`/`content.router`, replicates `app/main.py`'s
`protected_dependency = [Depends(verify_api_key), Depends(get_rate_limit)]`
locally - even though the pages routes carry no per-route
`@limiter.limit(...)` of their own (see `content.py`'s module docstring for
why: small, bounded payloads unlike blog's paginated list), the router-level
`protected_dependency` from `app/main.py` still applies to every route on
`router`, so this harness keeps it identical to its siblings rather than
inventing a leaner variant - and mints a valid admin session directly via
`create_admin_access_token` rather than a real HTTP login round trip).

Real local Mongo (`mongodb://localhost:27017`, db `pdfconverterai`), same as
every other test file in this suite. `content_pages` has a unique index on
`slug` (`content_pages_slug_unique`) created by
`app.core.database.ensure_indexes()` - this file calls it once per session
before any test runs, same as the blog/tool-metadata test files' own
`_ensure_content_indexes` fixture, so the slug-conflict test below exercises
the real `DuplicateKeyError` translation path.

Covers, roughly in this order:
  1. `app/schemas/content_page.py` - slug format validator, the five
     `PageBlock` content-shape validations (one passing case per block type,
     plus malformed-content rejection), `extra="forbid"` rejecting
     `published_at`/`slug` on Update, `status` defaulting to draft on Create.
  2. `app/services/content/content_pages_service.py` - every `published_at`
     stamping branch, `SLUG_RESERVED` rejection, slug-conflict, draft
     invisibility, unpaginated `list_all_pages`.
  3. `app/routers/content.py`'s pages routes - public route never leaks
     drafts, all 5 admin routes require `require_admin` (parametrized,
     mirrors `test_content_blog_posts.py`'s
     `test_admin_blog_route_requires_api_key_layer_wrong_category_403`-style
     tests), and the admin list route is confirmed unpaginated (a bare
     list, not a `{items, total, ...}` envelope).
"""
import logging
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from bson import ObjectId
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.database import db, ensure_indexes
from app.core.rate_limiter import limiter
from app.core.security import verify_api_key
from app.routers import content as content_router
from app.schemas.content_page import (
    ContentPageCreate,
    ContentPageUpdate,
    PageStatus,
)
from app.services.auth.token_service import create_admin_access_token
from app.services.content.content_pages_service import (
    PageNotFound,
    PageSlugConflict,
    SlugReserved,
    create_page,
    delete_page,
    get_by_slug,
    list_all_pages,
    update_page,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ADMIN_EMAIL = "seed-test-admin@pdfconverterai.com"
_ADMIN_COOKIE_NAME = "admin_session"

# MongoDB stores BSON datetimes at millisecond precision, truncating (not
# rounding) any sub-millisecond component - so a `published_at` round-tripped
# through Mongo can be up to ~1ms earlier than a microsecond-precision
# `before` timestamp captured in plain Python just before the write. This
# tolerance absorbs that storage-precision gap without weakening the
# assertion's actual intent (published_at was stamped "at roughly this
# moment", not "at this exact microsecond").
_MONGO_MS_TRUNCATION_TOLERANCE = timedelta(milliseconds=1)

logger = logging.getLogger(__name__)


# --- test app scaffolding (identical to test_content_tool_metadata.py) ----


async def _get_rate_limit(key_info: dict = Depends(verify_api_key)):
    key_data = key_info["key_data"]
    if key_data["type"] == "internal":
        return limiter.limit("100/minute")
    return limiter.limit(f"{key_data['rate_limit_per_day']}/day")


_protected_dependency = [Depends(verify_api_key), Depends(_get_rate_limit)]


def _build_test_app() -> FastAPI:
    app = FastAPI()
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
    key_value = f"content-pages-test-key-{ObjectId()}"
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
    key_value = f"wrong-category-pages-test-key-{ObjectId()}"
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
async def created_page_slugs():
    """Tracks every `content_pages.slug` a test creates (via the API or
    direct service call) and deletes exactly those documents after the
    test."""
    slugs: list[str] = []
    yield slugs
    if slugs:
        await db.content_pages.delete_many({"slug": {"$in": slugs}})


def _auth_headers(api_key_value: str) -> dict:
    return {"X-API-Key": api_key_value}


_HERO_BLOCK = {"type": "hero", "content": {"heading": "Welcome", "subheading": "A subheading"}}
_RICH_TEXT_BLOCK = {"type": "rich_text", "content": {"body": "Some **markdown** body text."}}
_IMAGE_BLOCK = {
    "type": "image",
    "content": {"url": "https://cdn.example.com/a.png", "alt_text": "A description", "caption": "A caption"},
}
_CTA_BANNER_BLOCK = {
    "type": "cta_banner",
    "content": {"message": "Sign up now", "style": "announcement", "link": {"label": "Go", "href": "/signup"}},
}
_AD_SLOT_BLOCK = {"type": "ad_slot", "content": {"placement_id": "sidebar-1", "height_px": 250}}


def _page_payload(slug: str, **overrides) -> dict:
    payload = {
        "slug": slug,
        "title": overrides.get("title", "Test Page"),
        "meta_description": overrides.get("meta_description", "A test page meta description."),
        "blocks": overrides.get("blocks", [_HERO_BLOCK]),
    }
    if "status" in overrides:
        payload["status"] = overrides["status"]
    return payload


def _create_model(slug: str, **overrides) -> ContentPageCreate:
    return ContentPageCreate(**_page_payload(slug, **overrides))


# ===========================================================================
# 1. Schema tests: app/schemas/content_page.py
# ===========================================================================


# --- slug format validator --------------------------------------------------


def test_slug_valid_lowercase_hyphenated_accepted():
    model = _create_model("a-valid-page-slug-123")
    assert model.slug == "a-valid-page-slug-123"


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
        _create_model(bad_slug)


# --- PageBlock content-shape validation: one passing case per block type ---


def test_hero_block_valid_content_accepted():
    model = _create_model("hero-block-test-1", blocks=[_HERO_BLOCK])
    assert model.blocks[0].type.value == "hero"
    assert model.blocks[0].content["heading"] == "Welcome"


def test_rich_text_block_valid_content_accepted():
    model = _create_model("rich-text-block-test-1", blocks=[_RICH_TEXT_BLOCK])
    assert model.blocks[0].type.value == "rich_text"
    assert "markdown" in model.blocks[0].content["body"]


def test_image_block_valid_content_accepted():
    model = _create_model("image-block-test-1", blocks=[_IMAGE_BLOCK])
    assert model.blocks[0].type.value == "image"
    assert model.blocks[0].content["alt_text"] == "A description"


def test_image_block_missing_alt_text_rejected():
    bad_block = {"type": "image", "content": {"url": "https://cdn.example.com/a.png"}}
    with pytest.raises(ValidationError):
        _create_model("image-block-no-alt-test-1", blocks=[bad_block])


@pytest.mark.parametrize("bad_url", ["javascript:alert(1)", "ftp://example.com/a.png", "//example.com/a.png"])
def test_image_block_non_http_scheme_rejected(bad_url):
    bad_block = {"type": "image", "content": {"url": bad_url, "alt_text": "Alt"}}
    with pytest.raises(ValidationError):
        _create_model("image-block-bad-scheme-test-1", blocks=[bad_block])


def test_cta_banner_block_valid_content_accepted():
    model = _create_model("cta-banner-block-test-1", blocks=[_CTA_BANNER_BLOCK])
    assert model.blocks[0].type.value == "cta_banner"
    assert model.blocks[0].content["message"] == "Sign up now"


def test_ad_slot_block_valid_content_accepted():
    model = _create_model("ad-slot-block-test-1", blocks=[_AD_SLOT_BLOCK])
    assert model.blocks[0].type.value == "ad_slot"
    assert model.blocks[0].content["placement_id"] == "sidebar-1"


def test_malformed_block_content_rejected():
    """A hero block whose content doesn't match HeroContent's shape
    (missing the required `heading` field) must be rejected by Pydantic
    before it ever reaches Mongo."""
    bad_block = {"type": "hero", "content": {"subheading": "Only a subheading, no heading"}}
    with pytest.raises(ValidationError):
        _create_model("malformed-block-test-1", blocks=[bad_block])


def test_blocks_requires_at_least_one_entry():
    with pytest.raises(ValidationError):
        _create_model("empty-blocks-test-1", blocks=[])


# --- extra="forbid" rejects published_at/slug on Update ---------------------


def test_update_schema_rejects_published_at_extra_field():
    with pytest.raises(ValidationError):
        ContentPageUpdate(title="New title", published_at="2020-01-01T00:00:00")


def test_update_schema_rejects_slug_extra_field():
    with pytest.raises(ValidationError):
        ContentPageUpdate(title="New title", slug="attempted-new-slug")


def test_update_schema_accepts_known_fields_only():
    model = ContentPageUpdate(title="New title", status=PageStatus.PUBLISHED)
    assert model.title == "New title"
    assert model.status == PageStatus.PUBLISHED


# --- status defaults to draft on Create -------------------------------------


def test_create_status_defaults_to_draft_when_omitted():
    model = _create_model("status-default-page-test-1")
    assert model.status == PageStatus.DRAFT


def test_create_status_explicit_published_accepted():
    model = _create_model("status-explicit-published-page-test-1", status="published")
    assert model.status == PageStatus.PUBLISHED


def test_create_schema_rejects_published_at_extra_field():
    """ContentPageCreate inherits extra="forbid" from ContentPageBase and
    deliberately does not declare `published_at` - a client-supplied value
    must be rejected outright, not silently accepted/ignored."""
    payload = _page_payload("create-rejects-published-at-page-test-1")
    payload["published_at"] = "2020-01-01T00:00:00"
    with pytest.raises(ValidationError):
        ContentPageCreate(**payload)


# ===========================================================================
# 2. Service layer tests: app/services/content/content_pages_service.py
# ===========================================================================


# --- SLUG_RESERVED rejection -------------------------------------------------


@pytest.mark.parametrize("reserved_slug", ["admin", "tools", "blog", "api"])
async def test_create_page_rejects_reserved_slug(reserved_slug):
    with pytest.raises(SlugReserved):
        await create_page(_create_model(reserved_slug))
    doc = await db.content_pages.find_one({"slug": reserved_slug})
    assert doc is None


# --- published_at stamping: create_page -------------------------------------


async def test_create_page_born_published_stamps_published_at(created_page_slugs):
    slug = "born-published-page-test-1"
    before = datetime.utcnow()
    page = await create_page(_create_model(slug, status="published"))
    after = datetime.utcnow()
    created_page_slugs.append(slug)

    assert page.status == PageStatus.PUBLISHED
    assert page.published_at is not None
    assert before - _MONGO_MS_TRUNCATION_TOLERANCE <= page.published_at <= after


async def test_create_page_draft_leaves_published_at_none(created_page_slugs):
    slug = "draft-create-page-test-1"
    page = await create_page(_create_model(slug))
    created_page_slugs.append(slug)

    assert page.status == PageStatus.DRAFT
    assert page.published_at is None


# --- published_at stamping: update_page (the three-condition rule) ---------


async def test_update_page_first_publish_stamps_published_at(created_page_slugs):
    slug = "first-publish-page-test-1"
    await create_page(_create_model(slug))
    created_page_slugs.append(slug)

    before = datetime.utcnow()
    updated = await update_page(slug, ContentPageUpdate(status=PageStatus.PUBLISHED))
    after = datetime.utcnow()

    assert updated.status == PageStatus.PUBLISHED
    assert updated.published_at is not None
    assert before - _MONGO_MS_TRUNCATION_TOLERANCE <= updated.published_at <= after


async def test_update_page_already_published_edit_does_not_restamp(created_page_slugs):
    slug = "no-restamp-page-test-1"
    await create_page(_create_model(slug, status="published"))
    created_page_slugs.append(slug)

    original = await get_by_slug(slug, include_drafts=True)
    assert original.published_at is not None

    # A plain content edit that also re-asserts status="published" (an
    # already-published page edited again) must not bump published_at.
    updated = await update_page(slug, ContentPageUpdate(title="Edited title", status=PageStatus.PUBLISHED))
    assert updated.published_at == original.published_at
    assert updated.title == "Edited title"


async def test_update_page_unpublish_does_not_clear_published_at(created_page_slugs):
    slug = "unpublish-page-test-1"
    await create_page(_create_model(slug, status="published"))
    created_page_slugs.append(slug)

    original = await get_by_slug(slug, include_drafts=True)
    assert original.published_at is not None

    updated = await update_page(slug, ContentPageUpdate(status=PageStatus.DRAFT))
    assert updated.status == PageStatus.DRAFT
    assert updated.published_at == original.published_at  # untouched, not cleared


async def test_update_page_republish_after_past_unpublish_does_not_restamp(created_page_slugs):
    slug = "republish-page-test-1"
    await create_page(_create_model(slug, status="published"))
    created_page_slugs.append(slug)

    first_published_at = (await get_by_slug(slug, include_drafts=True)).published_at

    await update_page(slug, ContentPageUpdate(status=PageStatus.DRAFT))
    republished = await update_page(slug, ContentPageUpdate(status=PageStatus.PUBLISHED))

    assert republished.status == PageStatus.PUBLISHED
    assert republished.published_at == first_published_at  # never bumped a second time


# --- slug uniqueness ---------------------------------------------------------


async def test_create_page_duplicate_slug_raises_conflict(created_page_slugs):
    slug = "dup-slug-page-service-test-1"
    await create_page(_create_model(slug, title="First"))
    created_page_slugs.append(slug)

    with pytest.raises(PageSlugConflict):
        await create_page(_create_model(slug, title="Second"))

    doc = await db.content_pages.find_one({"slug": slug})
    assert doc["title"] == "First"
    assert await db.content_pages.count_documents({"slug": slug}) == 1


# --- draft invisibility (get_by_slug) ---------------------------------------


async def test_get_by_slug_draft_raises_not_found_when_public(created_page_slugs):
    slug = "draft-invisible-page-test-1"
    await create_page(_create_model(slug))  # draft by default
    created_page_slugs.append(slug)

    with pytest.raises(PageNotFound):
        await get_by_slug(slug, include_drafts=False)


async def test_get_by_slug_draft_visible_to_admin(created_page_slugs):
    slug = "draft-visible-admin-page-test-1"
    await create_page(_create_model(slug))
    created_page_slugs.append(slug)

    page = await get_by_slug(slug, include_drafts=True)
    assert page.slug == slug
    assert page.status == PageStatus.DRAFT


async def test_get_by_slug_truly_missing_raises_not_found():
    with pytest.raises(PageNotFound):
        await get_by_slug("does-not-exist-page-service-test-1", include_drafts=False)


async def test_delete_page_removes_row_and_raises_on_second_delete():
    slug = "delete-page-service-test-1"
    await create_page(_create_model(slug))

    await delete_page(slug)
    doc = await db.content_pages.find_one({"slug": slug})
    assert doc is None

    with pytest.raises(PageNotFound):
        await delete_page(slug)


async def test_list_all_pages_includes_all_statuses_unpaginated(created_page_slugs):
    published_slug = "list-all-published-page-test-1"
    draft_slug = "list-all-draft-page-test-1"
    await create_page(_create_model(published_slug, status="published"))
    await create_page(_create_model(draft_slug))
    created_page_slugs.extend([published_slug, draft_slug])

    pages = await list_all_pages()
    slugs = {p.slug for p in pages}
    assert published_slug in slugs
    assert draft_slug in slugs


# ===========================================================================
# 3. HTTP tests: app/routers/content.py's pages routes
# ===========================================================================


async def test_public_get_page_published_returns_200(client, api_key, admin_cookie, created_page_slugs):
    slug = "public-get-published-page-test-1"
    create_resp = await client.post(
        "/v1/content/admin/pages",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_page_payload(slug, status="published", title="Big Page"),
    )
    assert create_resp.status_code == 200, create_resp.text
    created_page_slugs.append(slug)

    # No headers/cookies at all - confirms this route needs no auth.
    resp = await client.get(f"/v1/content/pages/{slug}")
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["slug"] == slug
    assert data["title"] == "Big Page"
    assert data["status"] == "published"


async def test_public_get_page_draft_returns_404_not_leaked(client, api_key, admin_cookie, created_page_slugs):
    slug = "public-draft-hidden-page-test-1"
    create_resp = await client.post(
        "/v1/content/admin/pages",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_page_payload(slug),  # draft by default
    )
    assert create_resp.status_code == 200, create_resp.text
    created_page_slugs.append(slug)

    resp = await client.get(f"/v1/content/pages/{slug}")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "PAGE_NOT_FOUND"


async def test_public_get_page_missing_slug_returns_404(client):
    resp = await client.get("/v1/content/pages/does-not-exist-public-page-test-1")
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "PAGE_NOT_FOUND"


async def test_page_public_route_is_registered_on_public_router_only():
    """Confirms the public route is actually declared on `public_router`
    (never gated by `protected_dependency`/`require_admin`) with only the
    expected HTTP method, rather than merely happening to work
    unauthenticated by accident."""
    public_paths = {route.path for route in content_router.public_router.routes}
    assert "/content/pages/{slug}" in public_paths

    public_detail_route = next(
        r for r in content_router.public_router.routes if r.path == "/content/pages/{slug}"
    )
    assert public_detail_route.methods == {"GET"}


# --- admin CRUD happy path ---------------------------------------------------


async def test_admin_create_list_get_update_delete_round_trip(client, api_key, admin_cookie, created_page_slugs):
    slug = "admin-crud-round-trip-page-test-1"
    create_resp = await client.post(
        "/v1/content/admin/pages",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_page_payload(slug, title="Original Title"),
    )
    assert create_resp.status_code == 200, create_resp.text
    created_page_slugs.append(slug)

    list_resp = await client.get(
        "/v1/content/admin/pages", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert list_resp.status_code == 200, list_resp.text
    # Unpaginated: `data` is a bare list, not a {items, total, ...} envelope.
    assert isinstance(list_resp.json()["data"], list)
    assert slug in {item["slug"] for item in list_resp.json()["data"]}

    get_resp = await client.get(
        f"/v1/content/admin/pages/{slug}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert get_resp.status_code == 200, get_resp.text
    assert get_resp.json()["data"]["title"] == "Original Title"

    update_resp = await client.put(
        f"/v1/content/admin/pages/{slug}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"title": "Updated Title"},
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["data"]["title"] == "Updated Title"

    first_delete = await client.delete(
        f"/v1/content/admin/pages/{slug}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert first_delete.status_code == 200, first_delete.text

    second_delete = await client.delete(
        f"/v1/content/admin/pages/{slug}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert second_delete.status_code == 404, second_delete.text
    assert second_delete.json()["error"]["code"] == "PAGE_NOT_FOUND"


async def test_admin_create_duplicate_slug_returns_409(client, api_key, admin_cookie, created_page_slugs):
    slug = "admin-dup-slug-http-page-test-1"
    first = await client.post(
        "/v1/content/admin/pages",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_page_payload(slug),
    )
    assert first.status_code == 200, first.text
    created_page_slugs.append(slug)

    second = await client.post(
        "/v1/content/admin/pages",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_page_payload(slug),
    )
    assert second.status_code == 409, second.text
    assert second.json()["error"]["code"] == "PAGE_SLUG_CONFLICT"


async def test_admin_create_reserved_slug_returns_400(client, api_key, admin_cookie):
    resp = await client.post(
        "/v1/content/admin/pages",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_page_payload("tools"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "SLUG_RESERVED"


async def test_admin_update_unknown_slug_returns_404(client, api_key, admin_cookie):
    resp = await client.put(
        "/v1/content/admin/pages/does-not-exist-update-http-page-test-1",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"title": "Doesn't matter"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "PAGE_NOT_FOUND"


# --- every admin write route (+ list/get) rejects bad/missing auth ---------


def _valid_create_body() -> dict:
    return _page_payload("auth-gate-page-test-slug-1")


def _valid_update_body() -> dict:
    return {"title": "Doesn't matter"}


_ADMIN_PAGE_ROUTES = [
    ("post", "/v1/content/admin/pages", _valid_create_body()),
    ("get", "/v1/content/admin/pages", None),
    ("get", "/v1/content/admin/pages/auth-gate-page-test-slug-1", None),
    ("put", "/v1/content/admin/pages/auth-gate-page-test-slug-1", _valid_update_body()),
    ("delete", "/v1/content/admin/pages/auth-gate-page-test-slug-1", None),
]


@pytest.mark.parametrize("method,path,json_body", _ADMIN_PAGE_ROUTES)
async def test_admin_page_route_requires_api_key_layer_missing_header_422(
    client, admin_cookie, method, path, json_body
):
    resp = await client.request(method.upper(), path, cookies=admin_cookie, json=json_body)
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_PAGE_ROUTES)
async def test_admin_page_route_requires_api_key_layer_invalid_key_403(
    client, admin_cookie, method, path, json_body
):
    resp = await client.request(
        method.upper(), path, headers=_auth_headers("not-a-real-key"), cookies=admin_cookie, json=json_body
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_PAGE_ROUTES)
async def test_admin_page_route_requires_api_key_layer_wrong_category_403(
    client, admin_cookie, wrong_category_api_key, method, path, json_body
):
    resp = await client.request(
        method.upper(), path, headers=_auth_headers(wrong_category_api_key), cookies=admin_cookie, json=json_body
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_PAGE_ROUTES)
async def test_admin_page_route_valid_api_key_but_no_cookie_401(client, api_key, method, path, json_body):
    resp = await client.request(method.upper(), path, headers=_auth_headers(api_key), json=json_body)
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_PAGE_ROUTES)
async def test_admin_page_route_valid_api_key_but_invalid_cookie_401(client, api_key, method, path, json_body):
    resp = await client.request(
        method.upper(),
        path,
        headers=_auth_headers(api_key),
        cookies={_ADMIN_COOKIE_NAME: "garbage-token"},
        json=json_body,
    )
    assert resp.status_code == 401, resp.text
