"""HTTP + service tests for the new `content` module (ADR-021 - Content
Model Foundation) - `app/routers/content.py` (`public_router` + `router`)
backed by `app/services/content/categories_service.py` and
`app/services/content/tags_service.py`.

Mirrors `tests/test_admin_homepage_sections.py`'s harness pattern exactly
(same reasons: `app.main` isn't importable in this checkout, so this file
builds its own tiny `FastAPI()` app mounting only `content.public_router`/
`content.router`, replicates `app/main.py`'s `protected_dependency =
[Depends(verify_api_key), Depends(get_rate_limit)]` locally, and mints a
valid admin session directly via `create_admin_access_token` rather than a
real HTTP login round trip). Categories and tags are combined into one file
(rather than split) because `tags` has no HTTP write surface of its own -
its only coverage is the direct service-layer unit test in the last section
below - so a separate `test_tags.py` would be nearly empty; this matches
`test_admin_homepage_sections.py` combining several sub-resources
(homepage sections + implicit tool_grid singleton rules) in one file too.

Real local Mongo (`mongodb://localhost:27017`, db `pdfconverterai`), same as
every other test file in this suite. `content_categories`/`tags` are brand
new collections with no indexes yet in this checkout (confirmed by direct
inspection - `db.content_categories.index_information()` returned `{}`
before this file's `_ensure_content_indexes` fixture ran), so this file
explicitly calls `app.core.database.ensure_indexes()` once per session
before any test runs - without it, `content_categories_slug_unique` and
`tags_slug_unique` would not exist yet and the CATEGORY_SLUG_CONFLICT /
tag-dedup tests below would silently pass for the wrong reason (no
uniqueness enforcement at all, rather than the service layer's intended
DuplicateKeyError translation).
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
from app.services.content.tags_service import get_or_create_tag, normalize_tag_slug

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ADMIN_EMAIL = "seed-test-admin@pdfconverterai.com"
_ADMIN_COOKIE_NAME = "admin_session"

logger = logging.getLogger(__name__)


# --- test app scaffolding ------------------------------------------------


async def _get_rate_limit(key_info: dict = Depends(verify_api_key)):
    """Local reimplementation of `app/main.py::get_rate_limit` - see module
    docstring for why this can't just be imported."""
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
    """Creates `content_categories_slug_unique`/`content_categories_order`/
    `tags_slug_unique` once for the whole test session - see module
    docstring. Idempotent (Mongo's create_index is a no-op for an
    already-existing equivalent definition), so safe even if a prior test
    run already created them."""
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
    """Same reasoning as tests/test_admin_homepage_sections.py's own
    fixture: `limiter` is a process-wide singleton, reset before/after every
    test in this module."""
    limiter.reset()
    yield
    limiter.reset()


@pytest_asyncio.fixture
async def api_key():
    """A fresh API key granted the `content` category only - genuinely
    exercises `verify_api_key`'s per-category check (`app/core/security.py`
    derives the category from the request path's second segment, i.e.
    "content" for `/v1/content/...`) rather than relying on an
    all-categories key that would never fail this check."""
    key_value = f"content-test-key-{ObjectId()}"
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
    """A valid, active API key that is NOT granted the `content` category -
    used to prove the per-category check actually fails closed, not just
    the "key exists at all" check."""
    key_value = f"wrong-category-test-key-{ObjectId()}"
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
    """A valid, never-expired admin session cookie header value."""
    token = create_admin_access_token(_ADMIN_EMAIL)
    return {_ADMIN_COOKIE_NAME: token}


@pytest_asyncio.fixture
async def created_category_ids():
    """Tracks every `content_categories._id` a test creates (via the API or
    direct DB insert for setup) and deletes exactly those documents after
    the test - never wipes the collection wholesale, matching
    `tests/conftest.py::_cleanup_api_key`'s "only clean up what you made"
    convention."""
    ids: list[ObjectId] = []
    yield ids
    if ids:
        await db.content_categories.delete_many({"_id": {"$in": ids}})


def _auth_headers(api_key_value: str) -> dict:
    return {"X-API-Key": api_key_value}


async def _insert_category_direct(
    label: str, slug: str, content_type: str, order: int, color_token: str | None = None
) -> ObjectId:
    """Bypasses the HTTP/service layer entirely for test setup - primarily
    used to seed `content_type='tool_metadata'` rows, which the service
    layer's `create_category` actively rejects (mirroring how the seed
    script itself bypasses the service layer)."""
    now = datetime.utcnow()
    doc = {
        "label": label,
        "slug": slug,
        "content_type": content_type,
        "color_token": color_token,
        "order": order,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.content_categories.insert_one(doc)
    return result.inserted_id


def _blog_payload(label: str, slug: str, order: int = 0) -> dict:
    return {"label": label, "slug": slug, "content_type": "blog", "color_token": None, "order": order}


# --- GET /v1/content/categories (public) ----------------------------------


async def test_public_list_includes_both_content_types_no_auth(client, created_category_ids):
    tool_id = await _insert_category_direct("PDF", "pdf-cat-test-1", "tool_metadata", 0, color_token="pdf")
    blog_id = await _insert_category_direct("Tips", "tips-cat-test-1", "blog", 0)
    created_category_ids.extend([tool_id, blog_id])

    resp = await client.get("/v1/content/categories")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    ids = {item["id"] for item in body["data"]}
    assert str(tool_id) in ids
    assert str(blog_id) in ids
    by_id = {item["id"]: item for item in body["data"]}
    assert by_id[str(tool_id)]["content_type"] == "tool_metadata"
    assert by_id[str(blog_id)]["content_type"] == "blog"


async def test_public_list_content_type_filter(client, created_category_ids):
    tool_id = await _insert_category_direct("Image", "image-cat-test-1", "tool_metadata", 0, color_token="image")
    blog_id = await _insert_category_direct("Product Updates", "product-updates-test-1", "blog", 0)
    created_category_ids.extend([tool_id, blog_id])

    resp = await client.get("/v1/content/categories", params={"content_type": "tool_metadata"})
    assert resp.status_code == 200
    data = resp.json()["data"]
    ids = {item["id"] for item in data}
    assert str(tool_id) in ids
    assert str(blog_id) not in ids
    assert all(item["content_type"] == "tool_metadata" for item in data)

    resp2 = await client.get("/v1/content/categories", params={"content_type": "blog"})
    assert resp2.status_code == 200
    data2 = resp2.json()["data"]
    ids2 = {item["id"] for item in data2}
    assert str(blog_id) in ids2
    assert str(tool_id) not in ids2


# --- GET /v1/content/tags (public) ----------------------------------------


async def test_public_list_tags_requires_no_auth(client):
    resp = await client.get("/v1/content/tags")
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert isinstance(resp.json()["data"], list)


# --- Dual auth-layer wiring (API key AND admin session, independently) ---


async def test_write_route_requires_api_key_layer_missing_header_422(client, admin_cookie):
    # No X-API-Key header at all -> FastAPI's own required-Header validation
    # fires before the route body ever runs.
    resp = await client.delete(f"/v1/content/categories/{ObjectId()}", cookies=admin_cookie)
    assert resp.status_code == 422


async def test_write_route_requires_api_key_layer_invalid_key_403(client, admin_cookie):
    resp = await client.delete(
        f"/v1/content/categories/{ObjectId()}",
        headers=_auth_headers("not-a-real-key"),
        cookies=admin_cookie,
    )
    assert resp.status_code == 403


async def test_write_route_requires_api_key_layer_wrong_category_403(client, admin_cookie, wrong_category_api_key):
    """A genuinely valid, active key - just not granted the `content`
    category - must still be rejected. Proves the per-category check, not
    just "does any active key exist"."""
    resp = await client.delete(
        f"/v1/content/categories/{ObjectId()}",
        headers=_auth_headers(wrong_category_api_key),
        cookies=admin_cookie,
    )
    assert resp.status_code == 403


async def test_write_route_valid_api_key_but_no_cookie_401(client, api_key):
    resp = await client.delete(f"/v1/content/categories/{ObjectId()}", headers=_auth_headers(api_key))
    assert resp.status_code == 401


async def test_write_route_valid_api_key_but_invalid_cookie_401(client, api_key):
    resp = await client.delete(
        f"/v1/content/categories/{ObjectId()}",
        headers=_auth_headers(api_key),
        cookies={_ADMIN_COOKIE_NAME: "garbage-token"},
    )
    assert resp.status_code == 401


async def test_write_route_valid_key_and_valid_cookie_succeeds(client, api_key, admin_cookie, created_category_ids):
    resp = await client.post(
        "/v1/content/categories",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload("Auth Layer OK", "auth-layer-ok-test-1"),
    )
    assert resp.status_code == 200, resp.text
    created_category_ids.append(ObjectId(resp.json()["data"]["id"]))


# --- The core read-only invariant (ADR-021) -------------------------------


async def test_create_tool_metadata_via_api_rejected_read_only(client, api_key, admin_cookie):
    resp = await client.post(
        "/v1/content/categories",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={
            "label": "Fake PDF",
            "slug": "fake-pdf-test-1",
            "content_type": "tool_metadata",
            "color_token": "pdf",
            "order": 0,
        },
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "CATEGORY_READ_ONLY"

    # Confirm nothing was actually inserted.
    doc = await db.content_categories.find_one({"slug": "fake-pdf-test-1"})
    assert doc is None


async def test_update_tool_metadata_category_rejected_read_only(client, api_key, admin_cookie, created_category_ids):
    tool_id = await _insert_category_direct("PDF", "pdf-readonly-test-1", "tool_metadata", 0, color_token="pdf")
    created_category_ids.append(tool_id)

    resp = await client.put(
        f"/v1/content/categories/{tool_id}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"label": "Renamed PDF"},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "CATEGORY_READ_ONLY"

    # Original document must be untouched by the rejected update.
    doc = await db.content_categories.find_one({"_id": tool_id})
    assert doc["label"] == "PDF"


async def test_delete_tool_metadata_category_rejected_read_only(client, api_key, admin_cookie, created_category_ids):
    tool_id = await _insert_category_direct("Image", "image-readonly-test-1", "tool_metadata", 0, color_token="image")
    created_category_ids.append(tool_id)  # survives the rejected delete - clean it up

    resp = await client.delete(
        f"/v1/content/categories/{tool_id}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "CATEGORY_READ_ONLY"

    doc = await db.content_categories.find_one({"_id": tool_id})
    assert doc is not None


async def test_reorder_including_tool_metadata_id_rejected_whole_batch(
    client, api_key, admin_cookie, created_category_ids
):
    tool_id = await _insert_category_direct("SEO", "seo-readonly-test-1", "tool_metadata", 5, color_token="seo")
    blog_id = await _insert_category_direct("Guides", "guides-readonly-test-1", "blog", 0)
    created_category_ids.extend([tool_id, blog_id])

    resp = await client.post(
        "/v1/content/categories/reorder",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"categories": [{"id": str(tool_id), "order": 0}, {"id": str(blog_id), "order": 1}]},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "CATEGORY_READ_ONLY"

    # Neither document's order should have moved - whole batch rejected.
    tool_doc = await db.content_categories.find_one({"_id": tool_id})
    blog_doc = await db.content_categories.find_one({"_id": blog_id})
    assert tool_doc["order"] == 5
    assert blog_doc["order"] == 0


# --- Blog category happy path ---------------------------------------------


async def test_create_blog_category_succeeds(client, api_key, admin_cookie, created_category_ids):
    resp = await client.post(
        "/v1/content/categories",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload("Tips", "tips-happy-test-1"),
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["content_type"] == "blog"
    assert body["label"] == "Tips"
    assert body["color_token"] is None
    created_category_ids.append(ObjectId(body["id"]))


async def test_rename_blog_category_succeeds(client, api_key, admin_cookie, created_category_ids):
    create_resp = await client.post(
        "/v1/content/categories",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload("Original Name", "rename-happy-test-1"),
    )
    category_id = create_resp.json()["data"]["id"]
    created_category_ids.append(ObjectId(category_id))

    update_resp = await client.put(
        f"/v1/content/categories/{category_id}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"label": "Renamed"},
    )
    assert update_resp.status_code == 200, update_resp.text
    assert update_resp.json()["data"]["label"] == "Renamed"
    assert update_resp.json()["data"]["slug"] == "rename-happy-test-1"


async def test_delete_blog_category_succeeds(client, api_key, admin_cookie):
    create_resp = await client.post(
        "/v1/content/categories",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload("Delete Me", "delete-happy-test-1"),
    )
    category_id = create_resp.json()["data"]["id"]

    resp = await client.delete(
        f"/v1/content/categories/{category_id}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    doc = await db.content_categories.find_one({"_id": ObjectId(category_id)})
    assert doc is None


# --- Duplicate slug -> 409 CATEGORY_SLUG_CONFLICT -------------------------


async def test_create_duplicate_slug_returns_409(client, api_key, admin_cookie, created_category_ids):
    first = await client.post(
        "/v1/content/categories",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload("First", "dup-slug-test-1"),
    )
    assert first.status_code == 200, first.text
    created_category_ids.append(ObjectId(first.json()["data"]["id"]))

    second = await client.post(
        "/v1/content/categories",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload("Second", "dup-slug-test-1"),
    )
    assert second.status_code == 409, second.text
    body = second.json()
    assert body["success"] is False
    assert body["error"]["code"] == "CATEGORY_SLUG_CONFLICT"


async def test_update_duplicate_slug_returns_409(client, api_key, admin_cookie, created_category_ids):
    first = await client.post(
        "/v1/content/categories",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload("Taken", "taken-slug-test-1"),
    )
    created_category_ids.append(ObjectId(first.json()["data"]["id"]))

    second = await client.post(
        "/v1/content/categories",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_blog_payload("Mover", "mover-slug-test-1"),
    )
    second_id = second.json()["data"]["id"]
    created_category_ids.append(ObjectId(second_id))

    update_resp = await client.put(
        f"/v1/content/categories/{second_id}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"slug": "taken-slug-test-1"},
    )
    assert update_resp.status_code == 409, update_resp.text
    assert update_resp.json()["error"]["code"] == "CATEGORY_SLUG_CONFLICT"


# --- Not-found id -> 404 CATEGORY_NOT_FOUND --------------------------------


async def test_update_nonexistent_id_returns_404(client, api_key, admin_cookie):
    resp = await client.put(
        f"/v1/content/categories/{ObjectId()}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"label": "Doesn't matter"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CATEGORY_NOT_FOUND"


async def test_delete_nonexistent_id_returns_404(client, api_key, admin_cookie):
    resp = await client.delete(
        f"/v1/content/categories/{ObjectId()}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CATEGORY_NOT_FOUND"


async def test_reorder_unknown_id_returns_404(client, api_key, admin_cookie):
    resp = await client.post(
        "/v1/content/categories/reorder",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"categories": [{"id": str(ObjectId()), "order": 0}]},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "CATEGORY_NOT_FOUND"


# --- Reorder renumbers to a consistent 0..N-1 range ------------------------


async def test_reorder_renormalizes_duplicate_and_out_of_range_orders(
    client, api_key, admin_cookie, created_category_ids
):
    id_a = await _insert_category_direct("A", "reorder-a-test-1", "blog", 0)
    id_b = await _insert_category_direct("B", "reorder-b-test-1", "blog", 0)
    id_c = await _insert_category_direct("C", "reorder-c-test-1", "blog", 0)
    created_category_ids.extend([id_a, id_b, id_c])

    # Duplicate order (5) for A and B (tie broken by request-array
    # position: A before B), and an out-of-range/large order (999) for C
    # which nonetheless sorts first because 1 < 5.
    resp = await client.post(
        "/v1/content/categories/reorder",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={
            "categories": [
                {"id": str(id_a), "order": 5},
                {"id": str(id_b), "order": 5},
                {"id": str(id_c), "order": 1},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]

    by_id = {item["id"]: item["order"] for item in data if item["id"] in {str(id_a), str(id_b), str(id_c)}}
    assert by_id[str(id_c)] == 0
    assert by_id[str(id_a)] == 1
    assert by_id[str(id_b)] == 2

    # Renumbered range must be a stable, contiguous 0..N-1 - no duplicates,
    # no leftover out-of-range values (999/5) surviving the reorder.
    orders = sorted(by_id.values())
    assert orders == [0, 1, 2]


# --- tags_service.get_or_create_tag normalization (direct unit test) ------


async def test_normalize_tag_slug_variants_produce_same_slug():
    assert normalize_tag_slug("SEO") == "seo"
    assert normalize_tag_slug("seo") == "seo"
    assert normalize_tag_slug(" Seo ") == "seo"
    assert normalize_tag_slug(" SEO Tips! ") == "seo-tips"


async def test_get_or_create_tag_normalizes_casing_and_does_not_duplicate():
    variants = ["SEO", "seo", " Seo ", "SEO"]
    created_docs = []
    try:
        for raw in variants:
            doc = await get_or_create_tag(raw)
            created_docs.append(doc)

        slugs = {doc.slug for doc in created_docs}
        ids = {doc.id for doc in created_docs}
        assert slugs == {"seo"}
        assert len(ids) == 1  # every variant resolved to the same document

        # First-seen casing ("SEO") must win the display label, never
        # overwritten by a later variant's casing.
        assert created_docs[0].label == "SEO"
        assert all(doc.label == "SEO" for doc in created_docs)

        count = await db.tags.count_documents({"slug": "seo"})
        assert count == 1
    finally:
        await db.tags.delete_many({"slug": "seo"})


# --- seed_content_categories.py idempotency + registry parity -------------


_EXPECTED_SEED_CATEGORIES = [
    ("PDF", "pdf"),
    ("Image", "image"),
    ("Unit Converter", "unit-converter"),
    ("Text", "text"),
    ("Miscellaneous", "miscellaneous"),
    ("Cyber Security", "cyber-security"),
    ("Downloaders", "downloaders"),
    ("SEO", "seo"),
    ("Web / Network", "web-network"),
    ("AI Tools", "ai-tools"),
]


async def test_seed_script_idempotent_and_matches_tools_registry():
    """Runs `scripts/seed_content_categories.py::_run` twice in-process
    (same DB connection this test file already uses, per Handbook - never
    shell out to a second Python process with its own Mongo connection when
    an in-process call does the same thing) and confirms:
    - exactly 10 `tool_metadata` rows exist after each run (not 20 after
      the second) - the actual idempotency guarantee.
    - the label/slug pairs exactly match `frontend/lib/tools-registry.ts`'s
      10 categories (see `_EXPECTED_SEED_CATEGORIES` above, transcribed
      directly from that file).
    """
    import scripts.seed_content_categories as seed_module

    try:
        exit_code_1 = await seed_module._run()
        assert exit_code_1 == 0
        count_after_first = await db.content_categories.count_documents({"content_type": "tool_metadata"})
        assert count_after_first == 10

        exit_code_2 = await seed_module._run()
        assert exit_code_2 == 0
        count_after_second = await db.content_categories.count_documents({"content_type": "tool_metadata"})
        assert count_after_second == 10, "re-running the seed script must not duplicate rows"

        docs = [
            doc
            async for doc in db.content_categories.find({"content_type": "tool_metadata"}).sort("order", 1)
        ]
        actual_pairs = [(doc["label"], doc["slug"]) for doc in docs]
        assert actual_pairs == _EXPECTED_SEED_CATEGORIES

        # color_token mirrors slug for every tool_metadata row (per the
        # seed script's own documented convention).
        assert all(doc["color_token"] == doc["slug"] for doc in docs)
        # order is a stable, contiguous 0..9 range matching registry order.
        assert [doc["order"] for doc in docs] == list(range(10))
    finally:
        await db.content_categories.delete_many({"content_type": "tool_metadata"})
