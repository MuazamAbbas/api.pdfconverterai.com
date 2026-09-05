"""HTTP + service tests for the Tools Metadata CMS (`content` module,
ADR-021's foundation — feature spec approved 2026-09-04, see
`docs/roadmap/SPRINT_STATUS.md`'s 2026-09-04 entry).

Mirrors `tests/test_content_categories.py`'s harness pattern exactly (same
reasons documented there: `app.main` isn't importable in this checkout, so
this file builds its own tiny `FastAPI()` app mounting only
`content.public_router`/`content.router`, replicates `app/main.py`'s
`protected_dependency = [Depends(verify_api_key), Depends(get_rate_limit)]`
locally, and mints a valid admin session directly via
`create_admin_access_token` rather than a real HTTP login round trip).

Covers `app/routers/content.py`'s tool-metadata routes, backed by
`app/services/content/tool_metadata_service.py` and
`app/schemas/content_tool_metadata.py`.

Real local Mongo (`mongodb://localhost:27017`, db `pdfconverterai`), same as
every other test file in this suite. `content_tool_metadata` has a unique
index on `slug` created by `app.core.database.ensure_indexes()` — this file
calls it once per session before any test runs, same as
`test_content_categories.py`'s `_ensure_content_indexes` fixture, so the
TOOL_METADATA_SLUG_CONFLICT test below exercises the real DuplicateKeyError
translation path rather than a service layer that would silently succeed
without any uniqueness enforcement at all.

`content_categories` rows with `content_type="tool_metadata"` are inserted
directly via `_insert_tool_category_direct` (bypassing
`categories_service.create_category`, which actively rejects that
content_type — see `test_content_categories.py`'s own
`_insert_category_direct` for the identical reasoning) rather than relying
on the real seeded rows from `scripts/seed_content_categories.py` being
present in this checkout, so these tests are self-contained and don't
silently pass/fail depending on whether the seed script has been run.
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
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.database import db, ensure_indexes
from app.core.rate_limiter import limiter
from app.core.security import verify_api_key
from app.routers import content as content_router
from app.services.auth.token_service import create_admin_access_token

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ADMIN_EMAIL = "seed-test-admin@pdfconverterai.com"
_ADMIN_COOKIE_NAME = "admin_session"

logger = logging.getLogger(__name__)


# --- test app scaffolding (identical to test_content_categories.py) -------


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
    key_value = f"content-tm-test-key-{ObjectId()}"
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
    key_value = f"wrong-category-tm-test-key-{ObjectId()}"
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
    `content_type="tool_metadata"` in this file) and deletes exactly those
    documents after the test."""
    ids: list[ObjectId] = []
    yield ids
    if ids:
        await db.content_categories.delete_many({"_id": {"$in": ids}})


@pytest_asyncio.fixture
async def created_tool_metadata_slugs():
    """Tracks every `content_tool_metadata.slug` a test creates (via the API
    or direct DB insert) and deletes exactly those documents after the
    test."""
    slugs: list[str] = []
    yield slugs
    if slugs:
        await db.content_tool_metadata.delete_many({"slug": {"$in": slugs}})


@pytest_asyncio.fixture
async def created_tag_slugs():
    """Tracks every `tags.slug` this file's tests cause to be created (via
    `get_or_create_tag` wiring through the tool-metadata create/update
    routes) and deletes exactly those documents after the test."""
    slugs: list[str] = []
    yield slugs
    if slugs:
        await db.tags.delete_many({"slug": {"$in": slugs}})


def _auth_headers(api_key_value: str) -> dict:
    return {"X-API-Key": api_key_value}


async def _insert_tool_category_direct(label: str, slug: str, order: int = 0) -> ObjectId:
    """Bypasses `categories_service.create_category` entirely (it actively
    rejects `content_type='tool_metadata'`) — mirrors
    `test_content_categories.py::_insert_category_direct`."""
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


async def _insert_tool_metadata_direct(slug: str, category: str, **overrides) -> dict:
    """Inserts a `content_tool_metadata` document directly, bypassing the
    service/schema layer — used for public-GET setup so that test doesn't
    depend on the admin write path also being correct."""
    now = datetime.utcnow()
    doc = {
        "slug": slug,
        "title": overrides.get("title", "Test Tool"),
        "category": category,
        "icon": overrides.get("icon", "FileText"),
        "description": overrides.get("description", "A tool used for testing."),
        "tags": overrides.get("tags", []),
        "how_to_use": overrides.get("how_to_use"),
        "faq": overrides.get("faq"),
        "ad_slot": overrides.get("ad_slot"),
        "created_at": now,
        "updated_at": now,
    }
    await db.content_tool_metadata.insert_one(doc)
    return doc


def _tm_payload(slug: str, category: str, **overrides) -> dict:
    payload = {
        "slug": slug,
        "title": overrides.get("title", "Test Tool"),
        "category": category,
        "icon": overrides.get("icon", "FileText"),
        "description": overrides.get("description", "A tool used for testing."),
        "tags": overrides.get("tags", []),
    }
    if "how_to_use" in overrides:
        payload["how_to_use"] = overrides["how_to_use"]
    if "faq" in overrides:
        payload["faq"] = overrides["faq"]
    if "ad_slot" in overrides:
        payload["ad_slot"] = overrides["ad_slot"]
    return payload


# --- 1. GET /v1/content/tool-metadata/{slug} (public, no auth) ------------


async def test_public_get_tool_metadata_returns_200_and_correct_shape_no_auth(
    client, created_category_ids, created_tool_metadata_slugs
):
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-public-test-1")
    created_category_ids.append(category_id)

    slug = "pdf-merger-public-test-1"
    await _insert_tool_metadata_direct(
        slug,
        "pdf-tm-public-test-1",
        title="PDF Merger",
        icon="Merge",
        description="Merge PDFs together.",
        tags=["pdf-tools"],
        how_to_use="Upload files, click merge.",
        faq="Q: is it free? A: yes.",
    )
    created_tool_metadata_slugs.append(slug)

    # No headers/cookies at all - confirms this route needs no auth.
    resp = await client.get(f"/v1/content/tool-metadata/{slug}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["slug"] == slug
    assert data["title"] == "PDF Merger"
    assert data["category"] == "pdf-tm-public-test-1"
    assert data["icon"] == "Merge"
    assert data["description"] == "Merge PDFs together."
    assert data["tags"] == ["pdf-tools"]
    assert data["how_to_use"] == "Upload files, click merge."
    assert data["faq"] == "Q: is it free? A: yes."
    assert "id" in data and "created_at" in data and "updated_at" in data


async def test_public_get_tool_metadata_missing_slug_returns_404_no_auth(client):
    resp = await client.get("/v1/content/tool-metadata/does-not-exist-test-1")
    assert resp.status_code == 404, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "TOOL_METADATA_NOT_FOUND"


async def test_tool_metadata_public_route_is_registered_on_public_router():
    """Confirms the public GET route is actually declared on
    `public_router` (never gated by `protected_dependency`/`require_admin`)
    rather than merely happening to work unauthenticated by accident."""
    paths = {route.path for route in content_router.public_router.routes}
    assert "/content/tool-metadata/{slug}" in paths
    admin_paths = {route.path for route in content_router.router.routes}
    assert "/content/tool-metadata/{slug}" in admin_paths  # PUT/DELETE live here too, different methods
    # The public router's version of this path must only expose GET.
    public_route = next(
        r for r in content_router.public_router.routes if r.path == "/content/tool-metadata/{slug}"
    )
    assert public_route.methods == {"GET"}


# --- 2. Admin POST create: valid category vs INVALID_CATEGORY -------------


async def test_create_tool_metadata_with_valid_category_succeeds(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-create-test-1")
    created_category_ids.append(category_id)

    slug = "pdf-splitter-create-test-1"
    resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "pdf-tm-create-test-1", title="PDF Splitter", icon="Split"),
    )
    assert resp.status_code == 200, resp.text
    created_tool_metadata_slugs.append(slug)
    body = resp.json()["data"]
    assert body["slug"] == slug
    assert body["title"] == "PDF Splitter"
    assert body["category"] == "pdf-tm-create-test-1"
    assert body["icon"] == "Split"

    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert doc is not None
    assert doc["category"] == "pdf-tm-create-test-1"


async def test_create_tool_metadata_ignores_client_supplied_timestamps(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    """Regression test for a security-reviewer finding: ContentToolMetadataCreate
    declares created_at/updated_at as real fields (extra="forbid" only blocks
    undeclared keys), so create_tool_metadata must explicitly overwrite
    whatever the client sends before insert - mirrors
    categories_service.create_category's identical override."""
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-timestamp-test-1")
    created_category_ids.append(category_id)

    slug = "pdf-merger-timestamp-test-1"
    spoofed = "2000-01-01T00:00:00"
    payload = _tm_payload(slug, "pdf-tm-timestamp-test-1")
    payload["created_at"] = spoofed
    payload["updated_at"] = spoofed

    before = datetime.utcnow()
    resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=payload,
    )
    after = datetime.utcnow()
    assert resp.status_code == 200, resp.text
    created_tool_metadata_slugs.append(slug)

    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert doc is not None
    assert before <= doc["created_at"] <= after
    assert before <= doc["updated_at"] <= after


async def test_create_tool_metadata_with_unknown_category_returns_400_invalid_category(
    client, api_key, admin_cookie, created_tool_metadata_slugs
):
    slug = "orphan-tool-create-test-1"
    resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "not-a-real-category-slug"),
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_CATEGORY"

    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert doc is None  # rejected write must not have been inserted


async def test_create_tool_metadata_with_blog_only_category_returns_400_invalid_category(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    """A category slug that exists but is `content_type="blog"` (not
    `tool_metadata`) must still be rejected - proves the validation filters
    by content_type, not just slug existence."""
    now = datetime.utcnow()
    blog_doc = {
        "label": "Tips",
        "slug": "blog-only-category-test-1",
        "content_type": "blog",
        "color_token": None,
        "order": 0,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.content_categories.insert_one(blog_doc)
    created_category_ids.append(result.inserted_id)

    slug = "wrong-content-type-category-test-1"
    resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "blog-only-category-test-1"),
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_CATEGORY"
    created_tool_metadata_slugs.append(slug)  # no-op if rejected, safe either way


# --- 3. Duplicate slug -> 409 TOOL_METADATA_SLUG_CONFLICT ------------------


async def test_create_duplicate_slug_returns_409(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    category_id = await _insert_tool_category_direct("Image", "image-tm-dup-test-1")
    created_category_ids.append(category_id)

    slug = "dup-slug-tm-test-1"
    first = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "image-tm-dup-test-1", title="First"),
    )
    assert first.status_code == 200, first.text
    created_tool_metadata_slugs.append(slug)

    second = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "image-tm-dup-test-1", title="Second"),
    )
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["success"] is False
    assert body["error"]["code"] == "TOOL_METADATA_SLUG_CONFLICT"

    # Original document must be untouched by the rejected duplicate create.
    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert doc["title"] == "First"
    assert await db.content_tool_metadata.count_documents({"slug": slug}) == 1


# --- 4. icon outside ALLOWED_TOOL_ICONS -> 422 (Pydantic ValidationError) --


async def test_create_tool_metadata_invalid_icon_returns_422(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-badicon-test-1")
    created_category_ids.append(category_id)

    slug = "bad-icon-create-test-1"
    resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "pdf-tm-badicon-test-1", icon="NotARealLucideIcon"),
    )
    assert resp.status_code == 422, resp.text
    created_tool_metadata_slugs.append(slug)  # no-op if rejected, safe either way

    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert doc is None


async def test_update_tool_metadata_invalid_icon_returns_422(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-badicon-update-test-1")
    created_category_ids.append(category_id)

    slug = "bad-icon-update-test-1"
    create_resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "pdf-tm-badicon-update-test-1"),
    )
    assert create_resp.status_code == 200, create_resp.text
    created_tool_metadata_slugs.append(slug)

    resp = await client.put(
        f"/v1/content/tool-metadata/{slug}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"icon": "NotARealLucideIcon"},
    )
    assert resp.status_code == 422, resp.text

    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert doc["icon"] == "FileText"  # unchanged from create's default


# --- 5. PUT update: fields update, slug immutable, 404 unknown slug -------


async def test_update_tool_metadata_updates_fields(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-update-test-1")
    created_category_ids.append(category_id)

    slug = "update-fields-test-1"
    create_resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "pdf-tm-update-test-1", title="Original Title", description="Original description."),
    )
    assert create_resp.status_code == 200, create_resp.text
    created_tool_metadata_slugs.append(slug)

    resp = await client.put(
        f"/v1/content/tool-metadata/{slug}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"title": "Updated Title", "description": "Updated description.", "how_to_use": "Do the thing."},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["slug"] == slug
    assert body["title"] == "Updated Title"
    assert body["description"] == "Updated description."
    assert body["how_to_use"] == "Do the thing."
    assert body["category"] == "pdf-tm-update-test-1"  # untouched field preserved


async def test_update_tool_metadata_cannot_change_slug(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-slugimmutable-test-1")
    created_category_ids.append(category_id)

    slug = "immutable-slug-test-1"
    create_resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "pdf-tm-slugimmutable-test-1"),
    )
    assert create_resp.status_code == 200, create_resp.text
    created_tool_metadata_slugs.append(slug)

    # ContentToolMetadataUpdate has no `slug` field and extra="forbid" -
    # posting one must be rejected by schema validation (422), never
    # silently ignored and never actually applied.
    resp = await client.put(
        f"/v1/content/tool-metadata/{slug}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"slug": "attempted-new-slug-test-1", "title": "Doesn't matter"},
    )
    assert resp.status_code == 422, resp.text

    # Original document must still exist under its original slug, untouched.
    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert doc is not None
    assert doc["title"] != "Doesn't matter"
    renamed = await db.content_tool_metadata.find_one({"slug": "attempted-new-slug-test-1"})
    assert renamed is None


async def test_update_tool_metadata_unknown_slug_returns_404(client, api_key, admin_cookie):
    resp = await client.put(
        "/v1/content/tool-metadata/does-not-exist-update-test-1",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"title": "Doesn't matter"},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "TOOL_METADATA_NOT_FOUND"


async def test_update_tool_metadata_invalid_category_returns_400(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-update-invalidcat-test-1")
    created_category_ids.append(category_id)

    slug = "update-invalid-category-test-1"
    create_resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "pdf-tm-update-invalidcat-test-1"),
    )
    assert create_resp.status_code == 200, create_resp.text
    created_tool_metadata_slugs.append(slug)

    resp = await client.put(
        f"/v1/content/tool-metadata/{slug}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"category": "not-a-real-category-slug"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "INVALID_CATEGORY"

    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert doc["category"] == "pdf-tm-update-invalidcat-test-1"  # unchanged by rejected update


# --- 6. DELETE removes row, 404 on second delete ---------------------------


async def test_delete_tool_metadata_removes_row_and_404s_on_second_delete(
    client, api_key, admin_cookie, created_category_ids
):
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-delete-test-1")
    created_category_ids.append(category_id)

    slug = "delete-me-tm-test-1"
    create_resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "pdf-tm-delete-test-1"),
    )
    assert create_resp.status_code == 200, create_resp.text

    first_delete = await client.delete(
        f"/v1/content/tool-metadata/{slug}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert first_delete.status_code == 200, first_delete.text
    assert first_delete.json()["success"] is True

    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert doc is None

    second_delete = await client.delete(
        f"/v1/content/tool-metadata/{slug}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert second_delete.status_code == 404, second_delete.text
    assert second_delete.json()["error"]["code"] == "TOOL_METADATA_NOT_FOUND"


# --- 7. Tags: raw strings normalize to canonical slugs, tags_service wired -


async def test_create_tool_metadata_normalizes_raw_tags_to_canonical_slugs(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs, created_tag_slugs
):
    category_id = await _insert_tool_category_direct("SEO", "seo-tm-tags-test-1")
    created_category_ids.append(category_id)

    slug = "tag-normalize-test-1"
    resp = await client.post(
        "/v1/content/tool-metadata",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_tm_payload(slug, "seo-tm-tags-test-1", tags=["SEO", "seo", "  Seo Tips "]),
    )
    assert resp.status_code == 200, resp.text
    created_tool_metadata_slugs.append(slug)
    created_tag_slugs.extend(["seo", "seo-tips"])

    body = resp.json()["data"]
    # No raw casing/whitespace must survive into the stored/returned tags -
    # every entry must be one of the two canonical slugs.
    assert set(body["tags"]) == {"seo", "seo-tips"}
    assert all(t == t.lower() and t.strip() == t for t in body["tags"])

    doc = await db.content_tool_metadata.find_one({"slug": slug})
    assert set(doc["tags"]) == {"seo", "seo-tips"}

    # get_or_create_tag wiring actually ran (not just a field that happens
    # to hold the right-shaped strings): exactly one canonical `tags`
    # document per distinct slug, first-seen casing ("SEO") preserved as
    # the display label.
    seo_tag = await db.tags.find_one({"slug": "seo"})
    assert seo_tag is not None
    assert seo_tag["label"] == "SEO"
    assert await db.tags.count_documents({"slug": "seo"}) == 1

    seo_tips_tag = await db.tags.find_one({"slug": "seo-tips"})
    assert seo_tips_tag is not None
    assert seo_tips_tag["label"] == "Seo Tips"


# --- 8 & 9. Admin GET list: auth-gated, returns created rows ---------------


async def test_admin_list_tool_metadata_returns_created_rows(
    client, api_key, admin_cookie, created_category_ids, created_tool_metadata_slugs
):
    category_id = await _insert_tool_category_direct("PDF", "pdf-tm-list-test-1")
    created_category_ids.append(category_id)

    slug_a = "list-test-a-1"
    slug_b = "list-test-b-1"
    for slug in (slug_a, slug_b):
        create_resp = await client.post(
            "/v1/content/tool-metadata",
            headers=_auth_headers(api_key),
            cookies=admin_cookie,
            json=_tm_payload(slug, "pdf-tm-list-test-1"),
        )
        assert create_resp.status_code == 200, create_resp.text
        created_tool_metadata_slugs.append(slug)

    resp = await client.get(
        "/v1/content/tool-metadata", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    slugs = {item["slug"] for item in body["data"]}
    assert slug_a in slugs
    assert slug_b in slugs


# --- 9. Every admin write route (+ list GET) rejects bad/missing auth -----


def _valid_create_body() -> dict:
    return _tm_payload("auth-gate-test-slug-1", "auth-gate-irrelevant-category")


def _valid_update_body() -> dict:
    return {"title": "Doesn't matter"}


_ADMIN_ROUTES = [
    ("post", "/v1/content/tool-metadata", _valid_create_body()),
    ("put", "/v1/content/tool-metadata/auth-gate-test-slug-1", _valid_update_body()),
    ("delete", "/v1/content/tool-metadata/auth-gate-test-slug-1", None),
    ("get", "/v1/content/tool-metadata", None),
]


@pytest.mark.parametrize("method,path,json_body", _ADMIN_ROUTES)
async def test_admin_route_requires_api_key_layer_missing_header_422(client, admin_cookie, method, path, json_body):
    resp = await client.request(method.upper(), path, cookies=admin_cookie, json=json_body)
    assert resp.status_code == 422, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_ROUTES)
async def test_admin_route_requires_api_key_layer_invalid_key_403(client, admin_cookie, method, path, json_body):
    resp = await client.request(
        method.upper(), path, headers=_auth_headers("not-a-real-key"), cookies=admin_cookie, json=json_body
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_ROUTES)
async def test_admin_route_requires_api_key_layer_wrong_category_403(
    client, admin_cookie, wrong_category_api_key, method, path, json_body
):
    resp = await client.request(
        method.upper(), path, headers=_auth_headers(wrong_category_api_key), cookies=admin_cookie, json=json_body
    )
    assert resp.status_code == 403, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_ROUTES)
async def test_admin_route_valid_api_key_but_no_cookie_401(client, api_key, method, path, json_body):
    resp = await client.request(method.upper(), path, headers=_auth_headers(api_key), json=json_body)
    assert resp.status_code == 401, resp.text


@pytest.mark.parametrize("method,path,json_body", _ADMIN_ROUTES)
async def test_admin_route_valid_api_key_but_invalid_cookie_401(client, api_key, method, path, json_body):
    resp = await client.request(
        method.upper(),
        path,
        headers=_auth_headers(api_key),
        cookies={_ADMIN_COOKIE_NAME: "garbage-token"},
        json=json_body,
    )
    assert resp.status_code == 401, resp.text
