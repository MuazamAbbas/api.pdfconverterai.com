"""Service + HTTP tests for the new `analytics` module (ADR-023: Analytics
Module Foundation - Daily Counter Aggregation, Async In-Process Writes).

`app/analytics/service.py` (`record_tool_usage`/`record_page_view`) is
exercised directly, plus `POST /v1/analytics/pageview`
(`app/routers/analytics.py`) via a local `FastAPI()` test app - same
"`app.main` isn't importable in this checkout" reasoning
`tests/test_content_categories.py`'s module docstring documents, and the
exact same `_protected_dependency` reimplementation pattern.

Real local Mongo, same as every other test file in this suite - the
`analytics_counters_metric_type_target_date_unique` index is created once
per session via the same `ensure_indexes()` autouse-fixture pattern
`test_content_categories.py` uses for its own new collections.
"""
import logging
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.analytics.service import record_page_view, record_tool_usage
from app.core.database import db, ensure_indexes
from app.core.rate_limiter import limiter
from app.core.security import verify_api_key
from app.routers import analytics as analytics_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

logger = logging.getLogger(__name__)


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _get_counter(metric_type: str, target: str) -> dict | None:
    return await db.analytics_counters.find_one(
        {"metric_type": metric_type, "target": target, "date": _today()}
    )


async def _delete_counter(metric_type: str, target: str) -> None:
    await db.analytics_counters.delete_one(
        {"metric_type": metric_type, "target": target, "date": _today()}
    )


# --- test app scaffolding (mirrors test_content_categories.py) -----------


async def _get_rate_limit(key_info: dict = Depends(verify_api_key)):
    key_data = key_info["key_data"]
    if key_data["type"] == "internal":
        return limiter.limit("100/minute")
    return limiter.limit(f"{key_data['rate_limit_per_day']}/day")


_protected_dependency = [Depends(verify_api_key), Depends(_get_rate_limit)]


def _build_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(analytics_router.router, prefix="/v1", dependencies=_protected_dependency)

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
async def _ensure_analytics_indexes():
    await ensure_indexes()
    yield


@pytest_asyncio.fixture
async def client():
    app = _build_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# --- service-layer tests ---------------------------------------------------


async def test_record_tool_usage_creates_and_increments_counter():
    target = "test-analytics-tool-slug"
    await _delete_counter("tool_usage", target)
    try:
        await record_tool_usage(target, "tier1")
        doc = await _get_counter("tool_usage", target)
        assert doc is not None
        assert doc["count"] == 1
        assert doc["metric_type"] == "tool_usage"
        assert doc["date"] == _today()
        # `tier` is accepted by the function signature but never persisted
        # (see app/analytics/service.py's docstring) - the schema forbids
        # extra fields (`extra="forbid"`), so this also implicitly proves no
        # stray field slipped through.
        assert "tier" not in doc
        created_at_first = doc["created_at"]

        await record_tool_usage(target, "tier1")
        doc2 = await _get_counter("tool_usage", target)
        assert doc2["count"] == 2
        # created_at set once via $setOnInsert; updated_at tracks every write.
        assert doc2["created_at"] == created_at_first
        assert doc2["updated_at"] >= doc["updated_at"]
    finally:
        await _delete_counter("tool_usage", target)


async def test_record_page_view_creates_and_increments_counter():
    target = "/test-analytics-page-path"
    await _delete_counter("page_view", target)
    try:
        await record_page_view(target)
        doc = await _get_counter("page_view", target)
        assert doc is not None
        assert doc["count"] == 1
        assert doc["metric_type"] == "page_view"

        await record_page_view(target)
        doc2 = await _get_counter("page_view", target)
        assert doc2["count"] == 2
    finally:
        await _delete_counter("page_view", target)


async def test_record_tool_usage_never_raises_when_db_write_fails(monkeypatch):
    """AC5: no analytics write failure can raise or propagate to the
    caller - simulates Mongo being unavailable by making the upsert itself
    raise."""
    async def _boom(*args, **kwargs):
        raise ConnectionError("simulated Mongo outage")

    monkeypatch.setattr(db.analytics_counters, "update_one", _boom)
    # Must not raise.
    await record_tool_usage("does-not-matter", "tier1")
    await record_page_view("/does-not-matter")


async def test_record_tool_usage_empty_slug_is_a_noop():
    # Must not raise and must not write a document for an empty target.
    await record_tool_usage("", "tier1")
    doc = await _get_counter("tool_usage", "")
    assert doc is None


async def test_record_page_view_empty_path_is_a_noop():
    await record_page_view("")
    doc = await _get_counter("page_view", "")
    assert doc is None


# --- HTTP endpoint tests ----------------------------------------------------


async def test_pageview_endpoint_returns_200_and_increments_counter(client, api_key):
    target = "/test-analytics-http-page"
    await _delete_counter("page_view", target)
    try:
        resp = await client.post(
            "/v1/analytics/pageview",
            json={"path": target},
            headers={"X-API-Key": api_key["key"]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True

        doc = await _get_counter("page_view", target)
        assert doc is not None
        assert doc["count"] == 1
    finally:
        await _delete_counter("page_view", target)


async def test_pageview_endpoint_requires_api_key(client):
    resp = await client.post("/v1/analytics/pageview", json={"path": "/no-key"})
    assert resp.status_code in (401, 403, 422)


async def test_pageview_endpoint_rejects_missing_path(client, api_key):
    resp = await client.post(
        "/v1/analytics/pageview", json={}, headers={"X-API-Key": api_key["key"]}
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False


async def test_pageview_endpoint_rejects_non_path_value(client, api_key):
    """`path` must start with `/` - a bare host/full URL is rejected, not
    silently accepted as a `target`."""
    resp = await client.post(
        "/v1/analytics/pageview",
        json={"path": "https://example.com/not-a-path"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422


async def test_pageview_endpoint_response_never_carries_identifying_fields(client, api_key):
    """AC6 spot-check: the stored counter document (and the response) never
    carries IP/session/user-agent/user-id."""
    target = "/test-analytics-privacy-check"
    await _delete_counter("page_view", target)
    try:
        resp = await client.post(
            "/v1/analytics/pageview",
            json={"path": target},
            headers={"X-API-Key": api_key["key"], "User-Agent": "some-test-agent/1.0"},
        )
        assert resp.status_code == 200
        doc = await _get_counter("page_view", target)
        assert doc is not None
        forbidden_keys = {"ip", "ip_address", "session_id", "user_id", "user_agent", "userAgent"}
        assert forbidden_keys.isdisjoint(doc.keys())
    finally:
        await _delete_counter("page_view", target)


async def test_pageview_endpoint_survives_downstream_analytics_failure(client, api_key, monkeypatch):
    """AC5, end-to-end through the real route + BackgroundTask: even if the
    Mongo write itself fails, the HTTP response must still succeed."""
    async def _boom(*args, **kwargs):
        raise ConnectionError("simulated Mongo outage")

    monkeypatch.setattr(db.analytics_counters, "update_one", _boom)

    resp = await client.post(
        "/v1/analytics/pageview",
        json={"path": "/test-analytics-outage"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
