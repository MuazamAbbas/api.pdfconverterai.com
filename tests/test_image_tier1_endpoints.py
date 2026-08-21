"""HTTP-level tests for the `image` module's two Tier 1, instant, no-Job
endpoints (Handbook Part I.2/C.9): `POST /image/file_info` and
`POST /image/color_palette` (`app/routers/image.py`).

Deliberately does **not** reuse `tests/conftest.py`'s shared `client`/
`test_app` fixtures: those build the full test app including a real `arq`
Redis connection pool (`app.state.arq_redis`) at fixture setup, for routers
that need to enqueue jobs. Neither endpoint under test here ever touches
`app.state.arq_redis` (they are single-shot, in-memory analysis with no Job
at all - see `app/routers/image.py`'s docstring), so requiring a live Redis
connection just to test them would make this module untestable on any
checkout without Redis reachable (this one included - see
`tests/test_files_jobs_image_flow.py`'s equivalent tradeoff note for why the
*Job*-creating `image_ocr`/`image_compress` endpoints do need it). This
mounts only the `image` router in its own minimal app, with the same three
global exception handlers `tests/conftest.py::build_test_app` registers, so
error-envelope conformance (Handbook Part C.5) is still exercised faithfully
- it only skips the unrelated `arq_redis` wiring these two endpoints don't
use.

Still uses the shared `api_key` fixture from `tests/conftest.py` (plain
Mongo, no Redis) for auth.
"""
import io

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from PIL import Image
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.routers import image as image_router

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _build_image_only_app() -> FastAPI:
    app = FastAPI()
    app.include_router(image_router.router, prefix="/v1")

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


def _png_bytes(size=(30, 20), color=(10, 20, 30)) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _jpeg_bytes(size=(30, 20), color=(200, 50, 10)) -> bytes:
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=90)
    return buf.getvalue()


# ---------------------------------------------------------------------------
# POST /image/file_info
# ---------------------------------------------------------------------------


async def test_file_info_happy_path_png(image_client, api_key):
    content = _png_bytes(size=(30, 20))
    resp = await image_client.post(
        "/v1/image/file_info",
        files={"file": ("photo.png", content, "image/png")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["width"] == 30
    assert data["height"] == 20
    assert data["format"] == "PNG"
    assert data["mode"] == "RGB"
    assert data["size_bytes"] == len(content)
    assert "size_kb" in data and "size_mb" in data


async def test_file_info_happy_path_jpeg(image_client, api_key):
    content = _jpeg_bytes(size=(30, 20))
    resp = await image_client.post(
        "/v1/image/file_info",
        files={"file": ("photo.jpg", content, "image/jpeg")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["format"] == "JPEG"
    assert body["data"]["width"] == 30
    assert body["data"]["height"] == 20


async def test_file_info_rejects_corrupt_file_content(image_client, api_key):
    """Passes the extension/magic-byte check (real PNG signature bytes) but
    isn't actually decodable - the router must turn Pillow's own exception
    into a fixed, client-safe 400, not leak Pillow's raw text (issue #39 /
    Batch 5 convention)."""
    corrupt = b"\x89PNG\r\n\x1a\n" + b"not actually a valid png body after the header"
    resp = await image_client.post(
        "/v1/image/file_info",
        files={"file": ("corrupt.png", corrupt, "image/png")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_INVALID_CONTENT"
    assert "Pillow" not in body["message"]
    assert "Traceback" not in body["message"]


async def test_file_info_rejects_wrong_extension(image_client, api_key):
    resp = await image_client.post(
        "/v1/image/file_info",
        files={"file": ("notes.txt", b"just some text", "text/plain")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_INVALID_TYPE"


# ---------------------------------------------------------------------------
# POST /image/color_palette
# ---------------------------------------------------------------------------


def _gradient_png_bytes(size=(60, 60)) -> bytes:
    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel(
                (x, y),
                (
                    int(255 * x / size[0]),
                    int(255 * y / size[1]),
                    int(255 * ((x + y) % size[0]) / size[0]),
                ),
            )
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


async def test_color_palette_happy_path_returns_six_sorted_colors(image_client, api_key):
    content = _gradient_png_bytes()
    resp = await image_client.post(
        "/v1/image/color_palette",
        files={"file": ("photo.png", content, "image/png")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    colors = body["data"]["colors"]
    assert len(colors) == 6
    for entry in colors:
        assert set(entry.keys()) == {"hex", "rgb", "percentage"}
    percentages = [c["percentage"] for c in colors]
    assert percentages == sorted(percentages, reverse=True)
    assert 95 <= sum(percentages) <= 100.5


async def test_color_palette_rejects_corrupt_file_content(image_client, api_key):
    corrupt = b"\x89PNG\r\n\x1a\n" + b"not actually a valid png body after the header"
    resp = await image_client.post(
        "/v1/image/color_palette",
        files={"file": ("corrupt.png", corrupt, "image/png")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_INVALID_CONTENT"
