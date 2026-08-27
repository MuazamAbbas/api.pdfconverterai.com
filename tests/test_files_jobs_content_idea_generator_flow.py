"""HTTP-level tests for the Content Idea Generator Tier 2 router endpoints
(`POST /ai_tools/content_idea_generator/upload`,
`POST /ai_tools/content_idea_generator`), mirroring
`tests/test_files_jobs_keyword_research_flow.py`'s upload -> create-job ->
poll shape exactly.

Scope: router + service + shared cost/abuse-protection layer only - the ARQ
worker task (`ai_content_idea_generator`) is exercised end-to-end (with a
monkeypatched `generate_content_ideas`) in
`tests/test_worker_retry_ai_content_idea_generator.py`, not here. These
tests stop at "job created / queued", never invoking a worker function.

Builds its own lightweight app rather than reusing `tests/conftest.py`'s
global `build_test_app()`, same rationale as the keyword research flow test
file this mirrors.
"""
import os

# Must happen before any `app.*` import, same as tests/conftest.py.
os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import pytest
import pytest_asyncio
from arq.connections import RedisSettings, create_pool
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.database import db
from app.routers import ai_tools as ai_tools_router
from app.routers import jobs as jobs_router
from app.services.ai import usage_limits as usage_limits_service
from tests.conftest import _cleanup_api_key, _make_api_key

pytestmark = pytest.mark.asyncio(loop_scope="session")


# --- test app / fixtures ----------------------------------------------------


def build_content_idea_generator_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ai_tools_router.router, prefix="/v1")
    app.include_router(jobs_router.router, prefix="/v1")

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


@pytest_asyncio.fixture
async def cig_test_app():
    app = build_content_idea_generator_test_app()
    app.state.arq_redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield app
    await app.state.arq_redis.close()


@pytest_asyncio.fixture
async def cig_client(cig_test_app):
    transport = ASGITransport(app=cig_test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def api_key():
    key = await _make_api_key(categories=["all"])
    yield key
    await _cleanup_api_key(key["id"])


@pytest_asyncio.fixture
async def other_api_key():
    key = await _make_api_key(categories=["all"])
    yield key
    await _cleanup_api_key(key["id"])


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_ai_tools_usage():
    """`ai_tools_usage` isn't covered by `_cleanup_api_key`'s cleanup - keep
    the daily-cap tests isolated from each other and from other suites
    sharing the collection, same as the keyword research flow test file."""
    yield
    await db.ai_tools_usage.delete_many({})


# --- upload endpoint ---------------------------------------------------


async def test_upload_topic_returns_file_id(cig_client, api_key):
    resp = await cig_client.post(
        "/v1/ai_tools/content_idea_generator/upload",
        json={"topic": "sustainable home gardening"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["file_id"]
    assert body["data"]["filename"]


async def test_upload_topic_rejects_empty(cig_client, api_key):
    resp = await cig_client.post(
        "/v1/ai_tools/content_idea_generator/upload",
        json={"topic": "   "},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "TEXT_INPUT_EMPTY"


# --- job-creation endpoint -----------------------------------------------


async def test_upload_then_content_idea_generator_creates_queued_job(cig_client, api_key):
    upload_resp = await cig_client.post(
        "/v1/ai_tools/content_idea_generator/upload",
        json={"topic": "sustainable home gardening"},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]

    resp = await cig_client.post(
        "/v1/ai_tools/content_idea_generator",
        json={"file_id": file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["status"] == "queued"
    job_id = body["data"]["job_id"]

    poll_resp = await cig_client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    assert poll_resp.status_code == 200
    poll_body = poll_resp.json()
    assert poll_body["data"]["status"] == "queued"
    assert poll_body["data"]["type"] == "ai_content_idea_generator"


async def test_content_idea_generator_rejects_nonexistent_file_id(cig_client, api_key):
    resp = await cig_client.post(
        "/v1/ai_tools/content_idea_generator",
        json={"file_id": "0123456789ab0123456789ab"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404, resp.text
    assert resp.json()["error"]["code"] == "FILE_NOT_FOUND"


async def test_content_idea_generator_denies_non_owner_file(cig_client, api_key, other_api_key):
    upload_resp = await cig_client.post(
        "/v1/ai_tools/content_idea_generator/upload",
        json={"topic": "seo tools"},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]

    resp = await cig_client.post(
        "/v1/ai_tools/content_idea_generator",
        json={"file_id": file_id},
        headers={"X-API-Key": other_api_key["key"]},
    )
    assert resp.status_code == 403, resp.text
    assert resp.json()["error"]["code"] == "FILE_FORBIDDEN"


def test_content_idea_generator_request_schema_has_no_model_field():
    """Schema-level proof `ContentIdeaGeneratorRequest` never accepts a
    client-supplied `model` - ADR-018 decision 2 (no per-request picker),
    reaffirmed explicitly for this tool by the approved feature-spec even
    though v1's version had a picker."""
    assert "model" not in ai_tools_router.ContentIdeaGeneratorRequest.model_fields


# --- daily cap enforcement (shared ai_tools-wide budget) --------------------


async def test_content_idea_generator_daily_cap_exceeded_returns_429(cig_client, api_key, monkeypatch):
    """Doesn't loop 20 real requests - patches the daily-limit constant down
    to 1 so the second request is the one that trips the cap, proving the
    429/AI_TOOLS_DAILY_LIMIT_EXCEEDED wiring without a slow test."""
    monkeypatch.setattr(usage_limits_service, "AI_TOOLS_DAILY_LIMIT", 1)

    upload_1 = await cig_client.post(
        "/v1/ai_tools/content_idea_generator/upload",
        json={"topic": "first request"},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id_1 = upload_1.json()["data"]["file_id"]
    first = await cig_client.post(
        "/v1/ai_tools/content_idea_generator",
        json={"file_id": file_id_1},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200, first.text

    upload_2 = await cig_client.post(
        "/v1/ai_tools/content_idea_generator/upload",
        json={"topic": "second request"},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id_2 = upload_2.json()["data"]["file_id"]
    second = await cig_client.post(
        "/v1/ai_tools/content_idea_generator",
        json={"file_id": file_id_2},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 429, second.text
    body = second.json()
    assert body["success"] is False
    assert body["error"]["code"] == "AI_TOOLS_DAILY_LIMIT_EXCEEDED"


async def test_content_idea_generator_daily_cap_is_shared_with_keyword_research(
    cig_client, api_key, monkeypatch
):
    """The daily cap is a shared `ai_tools`-wide budget (ADR-018 / the
    generically-named `check_and_increment_ai_tools_daily_usage`), not a
    per-tool budget - a Keyword Research request consuming the cap must
    also block a subsequent Content Idea Generator request for the same
    key."""
    monkeypatch.setattr(usage_limits_service, "AI_TOOLS_DAILY_LIMIT", 1)

    kr_upload = await cig_client.post(
        "/v1/ai_tools/keyword_research/upload",
        json={"seedKeyword": "running shoes"},
        headers={"X-API-Key": api_key["key"]},
    )
    kr_file_id = kr_upload.json()["data"]["file_id"]
    kr_resp = await cig_client.post(
        "/v1/ai_tools/keyword_research",
        json={"file_id": kr_file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    assert kr_resp.status_code == 200, kr_resp.text

    cig_upload = await cig_client.post(
        "/v1/ai_tools/content_idea_generator/upload",
        json={"topic": "running shoes"},
        headers={"X-API-Key": api_key["key"]},
    )
    cig_file_id = cig_upload.json()["data"]["file_id"]
    cig_resp = await cig_client.post(
        "/v1/ai_tools/content_idea_generator",
        json={"file_id": cig_file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    assert cig_resp.status_code == 429, cig_resp.text
    assert cig_resp.json()["error"]["code"] == "AI_TOOLS_DAILY_LIMIT_EXCEEDED"


async def test_content_idea_generator_daily_cap_is_per_key(cig_client, api_key, other_api_key, monkeypatch):
    """Two different API keys each get their own daily budget - the cap
    tripping for one key must not affect the other."""
    monkeypatch.setattr(usage_limits_service, "AI_TOOLS_DAILY_LIMIT", 1)

    async def _upload_and_generate(key: str, topic: str):
        upload_resp = await cig_client.post(
            "/v1/ai_tools/content_idea_generator/upload",
            json={"topic": topic},
            headers={"X-API-Key": key},
        )
        file_id = upload_resp.json()["data"]["file_id"]
        return await cig_client.post(
            "/v1/ai_tools/content_idea_generator",
            json={"file_id": file_id},
            headers={"X-API-Key": key},
        )

    resp_a1 = await _upload_and_generate(api_key["key"], "topic a1")
    assert resp_a1.status_code == 200, resp_a1.text

    resp_b1 = await _upload_and_generate(other_api_key["key"], "topic b1")
    assert resp_b1.status_code == 200, resp_b1.text  # a fresh key's own budget

    resp_a2 = await _upload_and_generate(api_key["key"], "topic a2")
    assert resp_a2.status_code == 429, resp_a2.text
