"""Coverage for `POST /v1/seo_tools/seo_audit` (Handbook Part D.1, feature-
spec approved 2026-08-31 - docs/roadmap/SPRINT_STATUS.md "feature-spec
approved: SEO Audit (last v1-parity tool)" entry).

Local, redis-free app fixture
------------------------------
This endpoint never touches `request.app.state.arq_redis` (Tier 1, no Job
System) - mirrors `tests/test_web_tools_uptime_dns_ssl.py`'s
`_build_web_tools_only_app()` rationale/docstring exactly, just for
`seo_tools_router`, to avoid depending on the real Redis instance
`tests/conftest.py`'s shared `client`/`test_app` fixtures require (not
available in this environment - see that file's docstring for the original
finding).

`app.services.seo.seo_audit.run_seo_audit` is monkeypatched for the
router-level tests here (isolating rate-limiting/error-mapping logic from
the service's own real network I/O, which is covered separately and in
depth by `tests/test_seo_audit_service.py`). The hourly-rate-limit tests use
the *real* `app.services.seo.usage_limits.check_and_increment_seo_audit_hourly_
usage` against the real local Mongo instance (available in this environment,
unlike Redis - see `tests/conftest.py`'s `api_key` fixture, which already
depends on it), then clean up the counter doc(s) they create.
"""
import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.routers.seo_tools as seo_tools_router
from app.core.database import db
from app.services.seo.usage_limits import SEO_AUDIT_HOURLY_LIMIT

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


def _build_seo_tools_only_app() -> FastAPI:
    app = FastAPI()
    app.include_router(seo_tools_router.router, prefix="/v1")

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

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={"success": False, "message": "Internal server error", "error": {"code": "INTERNAL_ERROR"}},
        )

    return app


@pytest.fixture
def seo_app():
    return _build_seo_tools_only_app()


@pytest.fixture
async def seo_client(seo_app):
    transport = ASGITransport(app=seo_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _cleanup_seo_usage(api_key_id) -> None:
    await db.seo_tools_usage.delete_many({"apiKeyId": str(api_key_id)})


_SAMPLE_REPORT = {
    "url": "https://example.com",
    "final_url": "https://example.com",
    "reachable": True,
    "error": None,
    "meta_tags": {"findings": []},
    "heading_structure": {"findings": []},
    "broken_links": {"findings": [], "checked": 0, "total_links_found": 0, "details": []},
    "sitemap": {"findings": []},
    "image_alt_text": {"findings": []},
    "page_speed": {"findings": [], "metrics": None},
}


# ===========================================================================
# Happy path / response shape
# ===========================================================================

async def test_seo_audit_happy_path_returns_all_six_categories(seo_client, api_key, monkeypatch):
    async def _fake_run_seo_audit(url):
        assert url == "https://example.com"
        return _SAMPLE_REPORT

    monkeypatch.setattr(seo_tools_router, "run_seo_audit", _fake_run_seo_audit)

    try:
        resp = await seo_client.post(
            "/v1/seo_tools/seo_audit",
            json={"url": "https://example.com"},
            headers={"X-API-Key": api_key["key"]},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        # This router's own convention (not web_tools.py's envelope()) -
        # raw dict success body, no {success, message, data} wrapper.
        assert "success" not in body
        for key in (
            "meta_tags", "heading_structure", "broken_links",
            "sitemap", "image_alt_text", "page_speed",
        ):
            assert key in body
    finally:
        await _cleanup_seo_usage(api_key["id"])


async def test_seo_audit_missing_api_key_returns_error(seo_client):
    resp = await seo_client.post("/v1/seo_tools/seo_audit", json={"url": "https://example.com"})
    assert resp.status_code in (401, 403, 422)


# ===========================================================================
# Validation errors -> 400 (this router's own convention)
# ===========================================================================

async def test_seo_audit_empty_url_returns_400(seo_client, api_key, monkeypatch):
    async def _fake_run_seo_audit(url):
        raise ValueError("URL is required")

    monkeypatch.setattr(seo_tools_router, "run_seo_audit", _fake_run_seo_audit)

    try:
        resp = await seo_client.post(
            "/v1/seo_tools/seo_audit",
            json={"url": "not-empty-but-service-rejects-it"},
            headers={"X-API-Key": api_key["key"]},
        )
        assert resp.status_code == 400, resp.text
    finally:
        await _cleanup_seo_usage(api_key["id"])


async def test_seo_audit_missing_url_field_returns_422(seo_client, api_key):
    resp = await seo_client.post(
        "/v1/seo_tools/seo_audit", json={}, headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    await _cleanup_seo_usage(api_key["id"])


# ===========================================================================
# 500 mapping - no leaked exception internals (Handbook Part C.10)
# ===========================================================================

async def test_seo_audit_generic_exception_returns_500_without_leaking_exception_text(
    seo_client, api_key, monkeypatch
):
    async def _boom(url):
        raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/seo_tools.py")

    monkeypatch.setattr(seo_tools_router, "run_seo_audit", _boom)

    try:
        resp = await seo_client.post(
            "/v1/seo_tools/seo_audit",
            json={"url": "https://example.com"},
            headers={"X-API-Key": api_key["key"]},
        )
        assert resp.status_code == 500
        assert _SECRET_MARKER not in resp.text
    finally:
        await _cleanup_seo_usage(api_key["id"])


# ===========================================================================
# Hourly rate limit (acceptance criterion #3) - real Mongo counter
# ===========================================================================

async def test_seo_audit_hourly_rate_limit_exceeded_returns_429(seo_client, api_key, monkeypatch):
    async def _fake_run_seo_audit(url):
        return _SAMPLE_REPORT

    monkeypatch.setattr(seo_tools_router, "run_seo_audit", _fake_run_seo_audit)

    try:
        last_resp = None
        for _ in range(SEO_AUDIT_HOURLY_LIMIT + 1):
            last_resp = await seo_client.post(
                "/v1/seo_tools/seo_audit",
                json={"url": "https://example.com"},
                headers={"X-API-Key": api_key["key"]},
            )

        assert last_resp.status_code == 429, last_resp.text
        body = last_resp.json()
        assert body["error"]["code"] == "SEO_AUDIT_RATE_LIMIT_EXCEEDED"
    finally:
        await _cleanup_seo_usage(api_key["id"])


async def test_seo_audit_rate_limit_is_scoped_per_api_key(seo_client, api_key, other_api_key, monkeypatch):
    """A second, distinct key must not be affected by the first key's usage
    - the counter is keyed `{apiKeyId, hourBucket}`, not global."""
    async def _fake_run_seo_audit(url):
        return _SAMPLE_REPORT

    monkeypatch.setattr(seo_tools_router, "run_seo_audit", _fake_run_seo_audit)

    try:
        for _ in range(SEO_AUDIT_HOURLY_LIMIT + 1):
            await seo_client.post(
                "/v1/seo_tools/seo_audit",
                json={"url": "https://example.com"},
                headers={"X-API-Key": api_key["key"]},
            )

        resp = await seo_client.post(
            "/v1/seo_tools/seo_audit",
            json={"url": "https://example.com"},
            headers={"X-API-Key": other_api_key["key"]},
        )
        assert resp.status_code == 200, resp.text
    finally:
        await _cleanup_seo_usage(api_key["id"])
        await _cleanup_seo_usage(other_api_key["id"])
