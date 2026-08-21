"""HTTP-level request-validation tests for `POST /image/compress`
(Handbook Part I.2) - invalid `level` rejection and the default-level
behavior when `level` is omitted from the request body
(`app/routers/image.py::compress_image`/`CompressRequest`).

Like `tests/test_image_tier1_endpoints.py`, this does not reuse
`tests/conftest.py`'s shared `client`/`test_app` fixtures, but for a
different reason than that module: `/image/compress` *does* need to enqueue
a real job (`request.app.state.arq_redis.enqueue_job(...)` in
`app/routers/image.py::_create_image_job`), which isn't reachable in this
checkout at all (no local Redis - see `tests/test_files_jobs_image_flow.py`'s
tradeoff note). Rather than skip request-validation coverage entirely, this
module wires `app.state.arq_redis` to a minimal in-memory fake that records
calls without touching a real network connection - `enqueue_job` itself is
`arq`'s own concern (already covered by `arq`'s own test suite, not this
codebase's), not something `app/routers/image.py` implements, so faking it
doesn't hide any of *this* router's own logic under test. Job creation
itself still goes through the real `create_job`/`get_job`
(`app/services/jobs/service.py`) against real local Mongo, so the actual
`params={"level": ...}` persisted on the job document is genuinely verified,
not assumed.
"""
import os

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.storage import STORAGE_PATH
from app.routers import image as image_router
from app.services.jobs.service import get_job

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeArqRedis:
    """Records `enqueue_job` calls instead of touching a real Redis
    connection - see module docstring for why."""

    def __init__(self):
        self.enqueued = []

    async def enqueue_job(self, function, *args, _job_id=None, **kwargs):
        self.enqueued.append({"function": function, "args": args, "job_id": _job_id})
        return None


def _build_image_only_app() -> FastAPI:
    app = FastAPI()
    app.include_router(image_router.router, prefix="/v1")
    app.state.arq_redis = _FakeArqRedis()

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
            content={
                "success": False,
                "message": "Invalid request",
                "error": {"code": "VALIDATION_ERROR"},
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "success": False,
                "message": "Internal server error",
                "error": {"code": "INTERNAL_ERROR"},
            },
        )

    return app


@pytest_asyncio.fixture
async def image_client():
    app = _build_image_only_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


async def _make_file_doc(owner_id, content: bytes, filename: str):
    import hashlib

    from app.core.database import db
    from app.schemas.file import FileCreate, FileDocument

    os.makedirs(STORAGE_PATH, exist_ok=True)
    storage_path = os.path.join(STORAGE_PATH, filename)
    with open(storage_path, "wb") as f:
        f.write(content)

    file_create = FileCreate(
        storagePath=storage_path,
        checksum=hashlib.sha256(content).hexdigest(),
        originalFilename=filename,
        sizeBytes=len(content),
        mimeType="image/jpeg",
        ownerApiKeyId=owner_id,
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    return FileDocument(**doc)


async def test_compress_rejects_invalid_level(image_client, api_key):
    file_doc = await _make_file_doc(
        api_key["id"], b"\xff\xd8\xffnot a real jpeg body", "level-invalid.jpg"
    )

    resp = await image_client.post(
        "/v1/image/compress",
        json={"file_id": str(file_doc.id), "level": "ultra"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "LEVEL_INVALID"


async def test_compress_defaults_level_to_medium_when_omitted(image_client, api_key):
    file_doc = await _make_file_doc(
        api_key["id"], b"\xff\xd8\xffnot a real jpeg body", "level-default.jpg"
    )

    resp = await image_client.post(
        "/v1/image/compress",
        json={"file_id": str(file_doc.id)},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["status"] == "queued"
    job_id = body["data"]["job_id"]

    job = await get_job(job_id)
    assert job.params == {"level": "medium"}


async def test_compress_rejects_nonexistent_file_id(image_client, api_key):
    resp = await image_client.post(
        "/v1/image/compress",
        json={"file_id": "0123456789ab0123456789ab", "level": "low"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_NOT_FOUND"
