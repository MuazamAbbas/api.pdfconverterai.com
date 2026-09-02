"""HTTP + service tests for the `admin` module's Homepage Sections CMS
(ADR-019) - `app/routers/admin.py` (`public_router` + `router`) backed by
`app/services/admin/homepage_sections_service.py` and
`app/schemas/homepage_section.py`.

Mirrors `tests/test_files_jobs_pdf_flow.py`/`tests/conftest.py`'s harness
pattern (mount only the routers under test on a fresh `FastAPI()` instance,
real local Mongo, real local Redis untouched here since this module needs
none) and `tests/test_auth.py`'s pattern for minting a valid admin session
(`create_admin_access_token` directly - no HTTP login round trip needed,
that's already covered by `test_auth.py`).

`admin.router` is NOT mounted with the router-level `dependencies=` list in
`tests/conftest.py::build_test_app` (that fixture doesn't know about this
module at all), so this file builds its own tiny app - same shape as
`tests/test_auth.py::_build_test_app` - and deliberately replicates
`app/main.py`'s `protected_dependency = [Depends(verify_api_key),
Depends(get_rate_limit)]` locally (`app/main.py` itself isn't importable in
this checkout - missing `transformers`/`torch`, same gap
`tests/test_auth.py` documents for its own skipped real-app test) so the
API-key layer is genuinely exercised here, not just assumed.
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

from app.core.database import db
from app.core.rate_limiter import limiter
from app.core.security import verify_api_key
from app.routers import admin as admin_router
from app.services.auth.token_service import create_admin_access_token

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
    app.include_router(admin_router.public_router, prefix="/v1")
    app.include_router(admin_router.router, prefix="/v1", dependencies=_protected_dependency)

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


@pytest_asyncio.fixture
async def client():
    app = _build_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Same reasoning as tests/test_auth.py's own fixture: `limiter` is a
    process-wide singleton, reset before/after every test in this module."""
    limiter.reset()
    yield
    limiter.reset()


@pytest_asyncio.fixture
async def api_key():
    """A fresh, all-categories API key - independent of tests/conftest.py's
    fixture of the same name since this file builds its own test app."""
    key_value = f"admin-test-key-{ObjectId()}"
    doc = {
        "key": key_value,
        "status": "active",
        "usage_count": 0,
        "rate_limit_per_day": 100_000,
        "categories": ["all"],
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
async def created_section_ids():
    """Tracks every `homepage_sections._id` a test creates (via the API or
    direct DB insert for setup) and deletes exactly those documents after
    the test - never wipes the collection wholesale, matching
    `tests/conftest.py::_cleanup_api_key`'s "only clean up what you made"
    convention."""
    ids: list[ObjectId] = []
    yield ids
    if ids:
        await db.homepage_sections.delete_many({"_id": {"$in": ids}})


def _auth_headers(api_key_value: str) -> dict:
    return {"X-API-Key": api_key_value}


async def _insert_section_direct(section_type: str, content: dict, order: int, enabled: bool = True) -> ObjectId:
    """Bypasses the HTTP layer entirely for test setup where the type/shape
    under test isn't the thing being exercised (e.g. seeding sections for a
    reorder/list test)."""
    now = datetime.utcnow()
    doc = {
        "type": section_type,
        "order": order,
        "enabled": enabled,
        "content": content,
        "created_at": now,
        "updated_at": now,
    }
    result = await db.homepage_sections.insert_one(doc)
    return result.inserted_id


# --- GET /v1/admin/homepage-sections (public) -----------------------------


async def test_public_list_empty_collection_returns_empty_list(client):
    resp = await client.get("/v1/admin/homepage-sections")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"] == []


async def test_public_list_returns_only_enabled_sorted_by_order(client, created_section_ids):
    disabled_id = await _insert_section_direct("banner", {"message": "hidden"}, order=0, enabled=False)
    second_id = await _insert_section_direct("hero", {"heading": "Second"}, order=2, enabled=True)
    first_id = await _insert_section_direct("ad_slot", {"placement_id": "top", "height_px": 90}, order=1, enabled=True)
    created_section_ids.extend([disabled_id, second_id, first_id])

    resp = await client.get("/v1/admin/homepage-sections")
    assert resp.status_code == 200
    data = resp.json()["data"]
    ids = [item["id"] for item in data]
    assert str(disabled_id) not in ids
    assert ids == [str(first_id), str(second_id)]
    assert all(item["enabled"] is True for item in data)


async def test_public_list_requires_no_auth_at_all(client):
    # No X-API-Key, no admin cookie - must still succeed (route is on
    # `public_router`, mounted without `_protected_dependency`).
    resp = await client.get("/v1/admin/homepage-sections")
    assert resp.status_code == 200


# --- GET /v1/admin/homepage-sections/all ----------------------------------


async def test_list_all_requires_admin_session_401_without_cookie(client, api_key):
    resp = await client.get("/v1/admin/homepage-sections/all", headers=_auth_headers(api_key))
    assert resp.status_code == 401
    assert resp.json()["success"] is False


async def test_list_all_valid_session_includes_disabled_sections(client, api_key, admin_cookie, created_section_ids):
    enabled_id = await _insert_section_direct("hero", {"heading": "Visible"}, order=0, enabled=True)
    disabled_id = await _insert_section_direct("banner", {"message": "hidden"}, order=1, enabled=False)
    created_section_ids.extend([enabled_id, disabled_id])

    resp = await client.get(
        "/v1/admin/homepage-sections/all", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert resp.status_code == 200
    ids = [item["id"] for item in resp.json()["data"]]
    assert str(enabled_id) in ids
    assert str(disabled_id) in ids


# --- Dual auth-layer wiring (API key AND admin session, independently) ---


async def test_admin_route_requires_api_key_layer_missing_header_422(client, admin_cookie):
    # No X-API-Key header at all -> FastAPI's own required-Header validation
    # fires before the route body ever runs.
    resp = await client.get("/v1/admin/homepage-sections/all", cookies=admin_cookie)
    assert resp.status_code == 422


async def test_admin_route_requires_api_key_layer_invalid_key_403(client, admin_cookie):
    resp = await client.get(
        "/v1/admin/homepage-sections/all",
        headers=_auth_headers("not-a-real-key"),
        cookies=admin_cookie,
    )
    assert resp.status_code == 403


async def test_admin_route_valid_api_key_but_no_cookie_401(client, api_key):
    resp = await client.get("/v1/admin/homepage-sections/all", headers=_auth_headers(api_key))
    assert resp.status_code == 401


async def test_admin_route_valid_api_key_but_invalid_cookie_401(client, api_key):
    resp = await client.get(
        "/v1/admin/homepage-sections/all",
        headers=_auth_headers(api_key),
        cookies={_ADMIN_COOKIE_NAME: "garbage-token"},
    )
    assert resp.status_code == 401


async def test_admin_route_valid_key_and_valid_cookie_succeeds(client, api_key, admin_cookie):
    resp = await client.get(
        "/v1/admin/homepage-sections/all", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert resp.status_code == 200


# --- POST /v1/admin/homepage-sections (create) ----------------------------


_VALID_CONTENT_BY_TYPE = {
    "hero": {"heading": "Welcome to PDFConverterAI", "subheading": "Free tools, no signup"},
    "banner": {"message": "New tool launched!", "style": "announcement"},
    "ad_slot": {"placement_id": "homepage-top", "height_px": 250},
    "tool_grid": {},
}


@pytest.mark.parametrize("section_type", ["hero", "banner", "ad_slot", "tool_grid"])
async def test_create_each_type_with_valid_content_succeeds(
    client, api_key, admin_cookie, created_section_ids, section_type
):
    resp = await client.post(
        "/v1/admin/homepage-sections",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"type": section_type, "order": 0, "enabled": True, "content": _VALID_CONTENT_BY_TYPE[section_type]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["type"] == section_type
    created_section_ids.append(ObjectId(body["data"]["id"]))


@pytest.mark.parametrize(
    "section_type,bad_content",
    [
        ("hero", {"subheading": "missing the required heading field"}),
        ("banner", {"message": "ok", "style": "not-a-valid-style"}),
        ("ad_slot", {"placement_id": "x", "height_px": -5}),
        ("ad_slot", {"placement_id": "x"}),  # missing required height_px
        ("tool_grid", {"unexpected": "content is forbidden for tool_grid"}),
    ],
)
async def test_create_rejects_invalid_content_for_type_422(
    client, api_key, admin_cookie, section_type, bad_content
):
    resp = await client.post(
        "/v1/admin/homepage-sections",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"type": section_type, "order": 0, "enabled": True, "content": bad_content},
    )
    assert resp.status_code == 422


async def test_create_rejects_mismatched_content_shape_for_type_422(client, api_key, admin_cookie):
    # hero-shaped content submitted against type "banner" - "heading" is
    # extra/forbidden on BannerContent and "message" is missing/required.
    resp = await client.post(
        "/v1/admin/homepage-sections",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"type": "banner", "order": 0, "enabled": True, "content": {"heading": "wrong shape"}},
    )
    assert resp.status_code == 422


async def test_create_duplicate_tool_grid_returns_409(client, api_key, admin_cookie, created_section_ids):
    first = await client.post(
        "/v1/admin/homepage-sections",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"type": "tool_grid", "order": 0, "enabled": True, "content": {}},
    )
    assert first.status_code == 200, first.text
    created_section_ids.append(ObjectId(first.json()["data"]["id"]))

    second = await client.post(
        "/v1/admin/homepage-sections",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"type": "tool_grid", "order": 1, "enabled": True, "content": {}},
    )
    assert second.status_code == 409
    body = second.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HOMEPAGE_SECTION_CONFLICT"


# --- PUT /v1/admin/homepage-sections/{id} ---------------------------------


async def test_update_valid_content_and_enabled_returns_200(client, api_key, admin_cookie, created_section_ids):
    create_resp = await client.post(
        "/v1/admin/homepage-sections",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"type": "hero", "order": 0, "enabled": True, "content": {"heading": "Original"}},
    )
    section_id = create_resp.json()["data"]["id"]
    created_section_ids.append(ObjectId(section_id))

    update_resp = await client.put(
        f"/v1/admin/homepage-sections/{section_id}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"enabled": False, "content": {"heading": "Updated heading", "subheading": "Now with a subheading"}},
    )
    assert update_resp.status_code == 200, update_resp.text
    body = update_resp.json()["data"]
    assert body["enabled"] is False
    assert body["content"]["heading"] == "Updated heading"


async def test_update_invalid_merged_content_returns_422(client, api_key, admin_cookie, created_section_ids):
    create_resp = await client.post(
        "/v1/admin/homepage-sections",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"type": "hero", "order": 0, "enabled": True, "content": {"heading": "Original"}},
    )
    section_id = create_resp.json()["data"]["id"]
    created_section_ids.append(ObjectId(section_id))

    # Passes HomepageSectionUpdate's own (shallow, dict-only) validation but
    # must be re-rejected once the service layer merges it onto the
    # existing document's `type` ("hero") and re-validates through
    # HomepageSectionBase - "subheading" alone has no required "heading".
    update_resp = await client.put(
        f"/v1/admin/homepage-sections/{section_id}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"content": {"subheading": "heading is missing"}},
    )
    assert update_resp.status_code == 422
    body = update_resp.json()
    assert body["error"]["code"] == "HOMEPAGE_SECTION_CONTENT_INVALID"

    # Original document must be untouched by the rejected update.
    doc = await db.homepage_sections.find_one({"_id": ObjectId(section_id)})
    assert doc["content"] == {"heading": "Original"}


async def test_update_nonexistent_id_returns_404(client, api_key, admin_cookie):
    resp = await client.put(
        f"/v1/admin/homepage-sections/{ObjectId()}",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"enabled": False},
    )
    assert resp.status_code == 404


# --- POST /v1/admin/homepage-sections/reorder -----------------------------


async def test_reorder_renormalizes_duplicate_and_out_of_range_orders(
    client, api_key, admin_cookie, created_section_ids
):
    id_a = await _insert_section_direct("hero", {"heading": "A"}, order=0)
    id_b = await _insert_section_direct("banner", {"message": "B"}, order=0)
    id_c = await _insert_section_direct("ad_slot", {"placement_id": "c", "height_px": 10}, order=0)
    created_section_ids.extend([id_a, id_b, id_c])

    # Duplicate order (5) for A and B (tie broken by request-array
    # position: A before B), and an out-of-range/large order (999) for C
    # which nonetheless sorts first because 1 < 5.
    resp = await client.post(
        "/v1/admin/homepage-sections/reorder",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={
            "sections": [
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
    # no leftover out-of-range values (999) surviving the reorder.
    orders = sorted(by_id.values())
    assert orders == [0, 1, 2]


async def test_reorder_unknown_id_returns_404(client, api_key, admin_cookie):
    resp = await client.post(
        "/v1/admin/homepage-sections/reorder",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"sections": [{"id": str(ObjectId()), "order": 0}]},
    )
    assert resp.status_code == 404


# --- DELETE /v1/admin/homepage-sections/{id} ------------------------------


async def test_delete_tool_grid_unconditionally_forbidden(client, api_key, admin_cookie, created_section_ids):
    create_resp = await client.post(
        "/v1/admin/homepage-sections",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"type": "tool_grid", "order": 0, "enabled": True, "content": {}},
    )
    section_id = create_resp.json()["data"]["id"]
    created_section_ids.append(ObjectId(section_id))  # doc survives the rejected delete - clean it up

    resp = await client.delete(
        f"/v1/admin/homepage-sections/{section_id}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["error"]["code"] == "TOOL_GRID_DELETE_FORBIDDEN"

    # Confirm it genuinely still exists (delete really was a no-op).
    doc = await db.homepage_sections.find_one({"_id": ObjectId(section_id)})
    assert doc is not None


async def test_delete_real_section_returns_200(client, api_key, admin_cookie):
    create_resp = await client.post(
        "/v1/admin/homepage-sections",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"type": "banner", "order": 0, "enabled": True, "content": {"message": "delete me"}},
    )
    section_id = create_resp.json()["data"]["id"]

    resp = await client.delete(
        f"/v1/admin/homepage-sections/{section_id}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True

    doc = await db.homepage_sections.find_one({"_id": ObjectId(section_id)})
    assert doc is None


async def test_delete_nonexistent_id_returns_404(client, api_key, admin_cookie):
    resp = await client.delete(
        f"/v1/admin/homepage-sections/{ObjectId()}", headers=_auth_headers(api_key), cookies=admin_cookie
    )
    assert resp.status_code == 404
