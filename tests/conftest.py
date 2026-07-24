"""Pytest scaffolding for the files/jobs/pdf lifecycle test suite (Handbook
Part D.1 unit-test layer, Part C.7).

`app.main` cannot be imported/booted as a whole in this checkout (missing
`app/models/`, several routers need heavy deps that aren't installed here -
see the task brief). So instead of importing the real app, this builds a
minimal FastAPI app that only mounts the routers under test
(`files`, `jobs`, `pdf`) plus the same three global exception handlers
`app/main.py` registers (`StarletteHTTPException`, `RequestValidationError`,
and the generic `Exception` catch-all), so error-envelope conformance
(Handbook Part C.5) is still exercised faithfully.

Uses the real local Mongo (`mongodb://localhost:27017`, db `pdfconverterai`
- same db both `app/core/database.py` and `app/core/security.py` hardcode)
and real local Redis (`redis://localhost:6379`) rather than mocking them,
per the task brief. Fixtures create their own API keys/files/jobs and clean
up everything they insert; they never touch collections/documents they
didn't create.
"""
import os

# Must happen before any `app.*` import - `app.core.config.Settings()` has
# no default for `database_url` and is evaluated at import time.
os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import uuid
from datetime import datetime

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
from app.routers import files as files_router
from app.routers import jobs as jobs_router
from app.routers import pdf as pdf_router


def build_test_app() -> FastAPI:
    """Mirrors the router mounting + exception handlers from `app/main.py`
    for just the three routers this task touched, without any of the
    startup-time model preloading / unrelated routers that make the real
    `app.main` unimportable in this environment."""
    app = FastAPI()
    app.include_router(files_router.router, prefix="/v1")
    app.include_router(jobs_router.router, prefix="/v1")
    app.include_router(pdf_router.router, prefix="/v1")

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
async def test_app():
    app = build_test_app()
    app.state.arq_redis = await create_pool(RedisSettings.from_dsn(settings.redis_url))
    yield app
    await app.state.arq_redis.close()


@pytest_asyncio.fixture
async def client(test_app):
    transport = ASGITransport(app=test_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _make_api_key(categories=None) -> dict:
    key_value = f"test-key-{uuid.uuid4()}"
    doc = {
        "key": key_value,
        "status": "active",
        "usage_count": 0,
        "rate_limit_per_day": 100_000,
        "categories": categories or ["all"],
        "type": "external",
        "created_at": datetime.utcnow(),
    }
    result = await db.api_keys.insert_one(doc)
    return {"key": key_value, "id": result.inserted_id}


async def _cleanup_api_key(key_id) -> None:
    """Delete everything this key fixture created: its jobs, its files (both
    on disk and in Mongo), and the key document itself. Never touches data
    this test run didn't create.

    Matches `ownerApiKeyId`/`fileId` against both the ObjectId and the str
    form of each id - see `tests/test_schema_objectid_serialization.py` for
    why: `FileCreate/JobCreate.model_dump(by_alias=True)` actually persists
    these PyObjectId fields as plain strings, not BSON ObjectIds, so a
    naive `{"ownerApiKeyId": key_id}` (ObjectId) query silently matches
    nothing and would leak every file/job this fixture creates.
    """
    owner_match = {"ownerApiKeyId": {"$in": [key_id, str(key_id)]}}
    file_ids = [f["_id"] async for f in db.files.find(owner_match, {"_id": 1})]
    storage_paths = [
        f["storagePath"] async for f in db.files.find(owner_match, {"storagePath": 1})
    ]
    if file_ids:
        file_id_match = {"$in": file_ids + [str(i) for i in file_ids]}
        await db.jobs.delete_many({"fileId": file_id_match})
        await db.files.delete_many({"_id": {"$in": file_ids}})
    for path in storage_paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError:
            pass
    await db.api_keys.delete_one({"_id": key_id})


@pytest_asyncio.fixture
async def api_key():
    """A single API key with access to all categories and a high rate limit."""
    key = await _make_api_key()
    yield key
    await _cleanup_api_key(key["id"])


@pytest_asyncio.fixture
async def other_api_key():
    """A second, distinct API key - for ownership/cross-key tests."""
    key = await _make_api_key()
    yield key
    await _cleanup_api_key(key["id"])


@pytest.fixture
def test_pdf_bytes() -> bytes:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(here, "test.pdf"), "rb") as f:
        return f.read()


@pytest.fixture
def corrupt_pdf_bytes() -> bytes:
    """Not a real PDF at all (just enough of a header to pass a naive sniff
    test), so both PyMuPDF and the PyPDF2 fallback in
    `app.services.pdf.convert.extract_text_from_pdf` fail to parse it -
    exercises the ADR-003 permanent-failure/no-retry path."""
    return b"%PDF-1.4\nthis is not a real pdf structure, just garbage bytes\n%%EOF"
