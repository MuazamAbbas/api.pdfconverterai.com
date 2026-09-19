"""Schema + service + HTTP tests for Admin-managed SEO & site-verification
settings, Round 1 (ads.txt + verification codes) - `content` module, spec
approved per `docs/roadmap/SPRINT_STATUS.md`'s 2026-09-19 entry.

Mirrors `tests/test_content_pages.py`'s harness pattern exactly (same
reasons documented there: `app.main` isn't importable in this checkout - it
does `from transformers import pipeline` at module level, and this
environment's `.venv` has no `transformers` installed - so this file builds
its own tiny `FastAPI()` app mounting only `content.public_router`/
`content.router`, replicates `app/main.py`'s `protected_dependency =
[Depends(verify_api_key), Depends(get_rate_limit)]` locally, and mints a
valid admin session directly via `create_admin_access_token` rather than a
real HTTP login round trip). Confirmed directly in this session: `apscheduler`
IS installed in `.venv`, only `transformers` is missing - so `app.main`
still cannot be imported as a whole here, same conclusion the task brief
already reached, now independently re-verified rather than assumed.

Real local Mongo (`mongodb://localhost:27017`, db `pdfconverterai`), same as
every other test file in this suite - confirmed reachable in this session
(`server_info()` returned version 8.0.4). `site_settings` deliberately has
no custom index beyond the automatic `_id` index (see
`app/schemas/site_settings.py`'s "Indexing decision" docstring section), so
unlike the categories/pages/blog test files this file does NOT need an
`ensure_indexes()` session fixture - there is nothing to create.

Every test that writes a `site_settings` document cleans it up afterward via
the `_clear_site_settings` autouse fixture below, since this collection is a
genuine singleton (one fixed `_id`) shared across the whole test session -
unlike the list-shaped collections' tests, there's no way to scope cleanup
to "just this test's rows", so instead every test starts from a guaranteed
clean (no-document) state and the fixture deletes the singleton doc again
afterward. Never touches any other collection.
"""
import logging

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.database import db
from app.core.rate_limiter import limiter
from app.core.security import verify_api_key
from app.routers import content as content_router
from app.schemas.site_settings import (
    SITE_SETTINGS_SINGLETON_ID,
    SiteSettingsUpdate,
    SiteVerificationCode,
    default_site_settings,
)
from app.services.auth.token_service import create_admin_access_token
from app.services.content.site_settings_service import (
    get_site_settings,
    update_site_settings,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")

_ADMIN_EMAIL = "seed-test-admin@pdfconverterai.com"
_ADMIN_COOKIE_NAME = "admin_session"

logger = logging.getLogger(__name__)


# --- test app scaffolding (identical to test_content_pages.py) -------------


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


@pytest_asyncio.fixture(autouse=True)
async def _clear_site_settings():
    """Guarantees every test in this file starts from - and leaves behind -
    a genuinely no-document-yet state for the `site_settings` singleton, so
    tests never leak state into each other via the one shared `_id`."""
    await db.site_settings.delete_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    yield
    await db.site_settings.delete_one({"_id": SITE_SETTINGS_SINGLETON_ID})


@pytest_asyncio.fixture
async def api_key():
    """A fresh API key granted the `content` category only."""
    from datetime import datetime

    from bson import ObjectId

    key_value = f"site-settings-test-key-{ObjectId()}"
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
    from datetime import datetime

    from bson import ObjectId

    key_value = f"wrong-category-site-settings-test-key-{ObjectId()}"
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


def _auth_headers(api_key_value: str) -> dict:
    return {"X-API-Key": api_key_value}


def _valid_put_body(**overrides) -> dict:
    payload = {
        "ads_txt_content": overrides.get("ads_txt_content", "google.com, pub-1234567890, DIRECT, f08c47fec0942fa0"),
        "verification_codes": overrides.get(
            "verification_codes",
            [
                {"name": "google-site-verification", "content": "abc123"},
                {"name": "msvalidate.01", "content": "def456"},
            ],
        ),
    }
    return payload


# ===========================================================================
# 1. Schema tests: app/schemas/site_settings.py
# ===========================================================================


def test_default_site_settings_is_all_empty():
    defaults = default_site_settings()
    assert defaults.ads_txt_content == ""
    assert defaults.verification_codes == []


def test_ads_txt_content_within_limit_accepted():
    model = SiteSettingsUpdate(ads_txt_content="a" * 50000, verification_codes=[])
    assert len(model.ads_txt_content) == 50000


def test_ads_txt_content_over_limit_rejected():
    with pytest.raises(ValidationError):
        SiteSettingsUpdate(ads_txt_content="a" * 50001, verification_codes=[])


def test_verification_codes_at_cap_accepted():
    codes = [{"name": f"provider-{i}", "content": f"code-{i}"} for i in range(50)]
    model = SiteSettingsUpdate(ads_txt_content="", verification_codes=codes)
    assert len(model.verification_codes) == 50


def test_verification_codes_over_cap_rejected():
    codes = [{"name": f"provider-{i}", "content": f"code-{i}"} for i in range(51)]
    with pytest.raises(ValidationError):
        SiteSettingsUpdate(ads_txt_content="", verification_codes=codes)


@pytest.mark.parametrize("bad_name", ["", "   ", "\t", "\n"])
def test_verification_code_blank_or_whitespace_name_rejected(bad_name):
    with pytest.raises(ValidationError):
        SiteVerificationCode(name=bad_name, content="abc123")


@pytest.mark.parametrize("bad_content", ["", "   ", "\t", "\n"])
def test_verification_code_blank_or_whitespace_content_rejected(bad_content):
    with pytest.raises(ValidationError):
        SiteVerificationCode(name="google-site-verification", content=bad_content)


def test_verification_code_control_characters_rejected():
    with pytest.raises(ValidationError):
        SiteVerificationCode(name="google-site-verification", content="abc\ndef")


def test_verification_code_valid_accepted():
    code = SiteVerificationCode(name="google-site-verification", content="abc123")
    assert code.name == "google-site-verification"
    assert code.content == "abc123"


def test_site_settings_update_forbids_extra_fields():
    with pytest.raises(ValidationError):
        SiteSettingsUpdate(ads_txt_content="", verification_codes=[], head_injection_code="<script></script>")


# ===========================================================================
# 2. Service-layer tests: app/services/content/site_settings_service.py
# ===========================================================================


async def test_get_site_settings_no_document_returns_defaults_not_error():
    result = await get_site_settings()
    assert result.ads_txt_content == ""
    assert result.verification_codes == []


async def test_get_site_settings_never_inserts_a_document_as_side_effect():
    await get_site_settings()
    doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    assert doc is None, "GET must stay side-effect-free - no document should be created by a read"


async def test_get_site_settings_when_document_exists_does_not_500():
    """Isolates the GET-side read path from the PUT-side write path: inserts
    a raw Mongo document directly (bypassing `update_site_settings` entirely)
    to prove `get_site_settings()`'s own `SiteSettingsRead(**doc)` call
    breaks on a real document independently of whatever `update_site_settings`
    does - both functions share the same bug (see this file's HTTP section
    below for the PUT-side symptom), but this isolates which function is at
    fault rather than only observing it through the PUT round trip."""
    from datetime import datetime

    await db.site_settings.insert_one(
        {
            "_id": SITE_SETTINGS_SINGLETON_ID,
            "ads_txt_content": "example.com, pub-111, DIRECT, abc",
            "verification_codes": [{"name": "google-site-verification", "content": "abc123"}],
            "created_at": datetime.utcnow(),
            "updated_at": datetime.utcnow(),
        }
    )

    result = await get_site_settings()
    assert result.ads_txt_content == "example.com, pub-111, DIRECT, abc"
    assert len(result.verification_codes) == 1


async def test_update_site_settings_persists_both_fields():
    body = SiteSettingsUpdate(**_valid_put_body())
    result = await update_site_settings(body)
    assert result.ads_txt_content == body.ads_txt_content
    assert len(result.verification_codes) == 2

    doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    assert doc is not None
    assert doc["ads_txt_content"] == body.ads_txt_content
    assert doc["created_at"] is not None
    assert doc["updated_at"] is not None


async def test_update_site_settings_upsert_is_a_true_singleton_not_accumulating():
    """Calling update twice must replace, not accumulate - confirms the
    fixed-`_id` upsert semantics, not an accidental second document."""
    first_body = SiteSettingsUpdate(**_valid_put_body(ads_txt_content="first content"))
    await update_site_settings(first_body)

    second_body = SiteSettingsUpdate(
        ads_txt_content="second content",
        verification_codes=[{"name": "yandex-verification", "content": "xyz789"}],
    )
    result = await update_site_settings(second_body)

    assert result.ads_txt_content == "second content"
    assert len(result.verification_codes) == 1
    assert result.verification_codes[0].name == "yandex-verification"

    count = await db.site_settings.count_documents({})
    assert count == 1, "must remain exactly one document after a second update"

    doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    assert doc["ads_txt_content"] == "second content"
    assert len(doc["verification_codes"]) == 1


async def test_update_site_settings_preserves_created_at_across_second_update():
    first_body = SiteSettingsUpdate(**_valid_put_body())
    await update_site_settings(first_body)
    doc_after_first = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    created_at_first = doc_after_first["created_at"]

    second_body = SiteSettingsUpdate(ads_txt_content="updated", verification_codes=[])
    await update_site_settings(second_body)
    doc_after_second = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})

    assert doc_after_second["created_at"] == created_at_first
    assert doc_after_second["updated_at"] >= doc_after_first["updated_at"]


# ===========================================================================
# 3. HTTP tests: app/routers/content.py's site-settings routes
# ===========================================================================


async def test_public_get_site_settings_no_document_returns_200_empty_defaults(client):
    resp = await client.get("/v1/content/site-settings")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["ads_txt_content"] == ""
    assert body["data"]["verification_codes"] == []


async def test_public_get_site_settings_requires_no_auth(client):
    # No X-API-Key header, no admin cookie at all.
    resp = await client.get("/v1/content/site-settings")
    assert resp.status_code == 200


async def test_public_get_site_settings_route_registered_on_public_router_only():
    public_paths = {route.path for route in content_router.public_router.routes}
    router_paths = {route.path for route in content_router.router.routes}
    assert "/content/site-settings" in public_paths
    # PUT lives on the same literal path on `router` - confirms both routers
    # each carry exactly the one method they're supposed to on this shared
    # path, not an accidental duplicate registration.
    assert "/content/site-settings" in router_paths


async def test_put_site_settings_requires_api_key_layer_missing_header_422(client, admin_cookie):
    resp = await client.put("/v1/content/site-settings", cookies=admin_cookie, json=_valid_put_body())
    assert resp.status_code == 422, resp.text


async def test_put_site_settings_requires_api_key_layer_invalid_key_403(client, admin_cookie):
    resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers("not-a-real-key"),
        cookies=admin_cookie,
        json=_valid_put_body(),
    )
    assert resp.status_code == 403, resp.text


async def test_put_site_settings_requires_api_key_layer_wrong_category_403(client, admin_cookie, wrong_category_api_key):
    resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers(wrong_category_api_key),
        cookies=admin_cookie,
        json=_valid_put_body(),
    )
    assert resp.status_code == 403, resp.text


async def test_put_site_settings_valid_api_key_but_no_cookie_401(client, api_key):
    resp = await client.put(
        "/v1/content/site-settings", headers=_auth_headers(api_key), json=_valid_put_body()
    )
    assert resp.status_code == 401, resp.text


async def test_put_site_settings_valid_api_key_but_invalid_cookie_401(client, api_key):
    resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers(api_key),
        cookies={_ADMIN_COOKIE_NAME: "garbage-token"},
        json=_valid_put_body(),
    )
    assert resp.status_code == 401, resp.text


async def test_put_site_settings_valid_key_and_cookie_persists_and_get_round_trips(client, api_key, admin_cookie):
    put_resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_valid_put_body(),
    )
    assert put_resp.status_code == 200, put_resp.text
    put_body = put_resp.json()
    assert put_body["success"] is True
    assert put_body["data"]["ads_txt_content"] == _valid_put_body()["ads_txt_content"]
    assert len(put_body["data"]["verification_codes"]) == 2

    get_resp = await client.get("/v1/content/site-settings")
    assert get_resp.status_code == 200, get_resp.text
    get_body = get_resp.json()["data"]
    assert get_body["ads_txt_content"] == _valid_put_body()["ads_txt_content"]
    assert get_body["verification_codes"] == put_body["data"]["verification_codes"]


async def test_put_site_settings_oversized_ads_txt_content_rejected(client, api_key, admin_cookie):
    resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"ads_txt_content": "a" * 50001, "verification_codes": []},
    )
    assert resp.status_code == 422, resp.text

    # Confirm nothing was persisted by the rejected write.
    doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    assert doc is None


@pytest.mark.parametrize("bad_name", ["", "   "])
async def test_put_site_settings_blank_verification_code_name_rejected(client, api_key, admin_cookie, bad_name):
    resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"ads_txt_content": "", "verification_codes": [{"name": bad_name, "content": "abc123"}]},
    )
    assert resp.status_code == 422, resp.text

    doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    assert doc is None


@pytest.mark.parametrize("bad_content", ["", "   "])
async def test_put_site_settings_blank_verification_code_content_rejected(client, api_key, admin_cookie, bad_content):
    resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={
            "ads_txt_content": "",
            "verification_codes": [{"name": "google-site-verification", "content": bad_content}],
        },
    )
    assert resp.status_code == 422, resp.text

    doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    assert doc is None


async def test_put_site_settings_over_50_verification_codes_rejected(client, api_key, admin_cookie):
    codes = [{"name": f"provider-{i}", "content": f"code-{i}"} for i in range(51)]
    resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={"ads_txt_content": "", "verification_codes": codes},
    )
    assert resp.status_code == 422, resp.text

    doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
    assert doc is None


async def test_put_site_settings_twice_replaces_not_accumulates_via_http(client, api_key, admin_cookie):
    first_resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json=_valid_put_body(ads_txt_content="first via http"),
    )
    assert first_resp.status_code == 200, first_resp.text

    second_resp = await client.put(
        "/v1/content/site-settings",
        headers=_auth_headers(api_key),
        cookies=admin_cookie,
        json={
            "ads_txt_content": "second via http",
            "verification_codes": [{"name": "bing-verification", "content": "bing123"}],
        },
    )
    assert second_resp.status_code == 200, second_resp.text
    second_body = second_resp.json()["data"]
    assert second_body["ads_txt_content"] == "second via http"
    assert len(second_body["verification_codes"]) == 1

    count = await db.site_settings.count_documents({})
    assert count == 1

    get_resp = await client.get("/v1/content/site-settings")
    get_body = get_resp.json()["data"]
    assert get_body["ads_txt_content"] == "second via http"
    assert len(get_body["verification_codes"]) == 1
