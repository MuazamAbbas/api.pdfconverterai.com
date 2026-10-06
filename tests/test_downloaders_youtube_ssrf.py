"""Regression coverage for `api.pdfconverterai.com#52` - the YouTube
Downloader previously had zero SSRF protection: `app/routers/downloaders.py`'s
`POST /downloaders/upload` and `app/services/downloaders/processors.py`'s
`DownloadersYoutubeProcessor.validate()` only ever checked
`url.startswith(("http://", "https://"))`, with no `assert_host_is_safe()`
call anywhere before `yt_dlp.YoutubeDL(...).extract_info(url, download=True)`
would eventually run.

Mirrors `tests/test_web_tools_uptime_dns_ssl.py`'s SSRF-target coverage: the
same five-address `SSRF_TARGETS` set, exercised through the *real*, unmocked
`assert_host_is_safe()` - `socket.getaddrinfo()` resolves an IP-literal to
itself with no network I/O, so this is a real (not simulated) exercise of
the guard, not a stand-in for it.

Local, redis-free app fixture
------------------------------
`POST /downloaders/upload` never touches `request.app.state.arq_redis`
(only `POST /downloaders/youtube` does, which is out of scope for this
file) - same reasoning and pattern as `tests/test_web_tools_uptime_dns_ssl.py`'s
`_build_web_tools_only_app()`, which this file mirrors rather than importing
(that fixture mounts `web_tools_router`, this one needs `downloaders_router`).
Uses the real `api_key` fixture (`tests/conftest.py`) - backed by real local
Mongo, not Redis - since `save_text_input` persists a real `files` document;
the fixture's own teardown cleans up everything this file's tests create.
"""
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.routers.downloaders as downloaders_router
from app.services.downloaders.processors import DownloadersYoutubeProcessor
from app.services.jobs.processor import PermanentProcessingError

pytestmark = pytest.mark.asyncio(loop_scope="session")

SSRF_TARGETS = ["127.0.0.1", "169.254.169.254", "10.0.0.5", "192.168.1.1", "::1"]


def _build_downloaders_only_app() -> FastAPI:
    app = FastAPI()
    app.include_router(downloaders_router.router, prefix="/v1")

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
                "success": False, "message": "Invalid request",
                "error": {"code": "VALIDATION_ERROR"},
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "success": False, "message": "Internal server error",
                "error": {"code": "INTERNAL_ERROR"},
            },
        )

    return app


@pytest.fixture
def downloaders_app():
    app = _build_downloaders_only_app()
    # /downloaders/upload never touches arq_redis - a plain AsyncMock
    # stand-in sidesteps the real-Redis requirement (see module docstring).
    app.state.arq_redis = AsyncMock()
    return app


@pytest.fixture
async def downloaders_client(downloaders_app):
    transport = ASGITransport(app=downloaders_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ===========================================================================
# POST /downloaders/upload - router-level guard
# ===========================================================================

@pytest.mark.parametrize("target", SSRF_TARGETS)
async def test_upload_rejects_ssrf_targets_before_any_download_attempt(
    downloaders_client, api_key, target, monkeypatch
):
    # The guard must reject before `save_text_input` ever runs - no `files`
    # document (and therefore no possible `/downloaders/youtube` job) should
    # be created for an unsafe host.
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("save_text_input must not be called for an unsafe host")

    monkeypatch.setattr(downloaders_router, "save_text_input", _fail_if_called)

    url = f"http://[{target}]/video" if ":" in target else f"http://{target}/video"
    resp = await downloaders_client.post(
        "/v1/downloaders/upload",
        json={"url": url},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "URL_INVALID"
    assert body["message"] == "Cannot download from internal or reserved network addresses"


async def test_upload_still_rejects_missing_scheme(downloaders_client, api_key):
    """Pre-existing scheme check (unrelated to the SSRF guard) must still
    work unchanged - the new guard runs after it, not instead of it."""
    resp = await downloaders_client.post(
        "/v1/downloaders/upload",
        json={"url": "not-a-valid-url"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "URL_INVALID"
    assert body["message"] == "URL must start with http:// or https://"


async def test_upload_accepts_safe_public_url(downloaders_client, api_key):
    """Happy path must be unchanged: a normal public URL still uploads
    successfully (real, unmocked `assert_host_is_safe()` against a real
    public IP literal - no network I/O, `getaddrinfo()` resolves an
    IP-literal to itself)."""
    resp = await downloaders_client.post(
        "/v1/downloaders/upload",
        json={"url": "http://93.184.216.34/video"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["file_id"]


# ===========================================================================
# DownloadersYoutubeProcessor.validate() - job-processor-level guard
#
# Re-validated independently of the upload-time guard above (Handbook Part
# C.4 - every processor's own `validate()` step never trusts that
# upload-time validation is the only gate a `files` document could have
# reached it through).
# ===========================================================================

def _fake_file_doc(tmp_path, url: str):
    path = tmp_path / "url_input.txt"
    path.write_text(url, encoding="utf-8")

    class _FakeFileDoc:
        storagePath = str(path)
        originalFilename = "url_input.txt"

    return _FakeFileDoc()


@pytest.mark.parametrize("target", SSRF_TARGETS)
async def test_processor_validate_rejects_ssrf_targets(tmp_path, target):
    url = f"http://[{target}]/video" if ":" in target else f"http://{target}/video"
    file_doc = _fake_file_doc(tmp_path, url)

    with pytest.raises(PermanentProcessingError, match="internal or reserved"):
        await DownloadersYoutubeProcessor().validate(job=None, file_doc=file_doc)


async def test_processor_validate_still_rejects_missing_scheme(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "not-a-valid-url")

    with pytest.raises(PermanentProcessingError, match="http:// or https://"):
        await DownloadersYoutubeProcessor().validate(job=None, file_doc=file_doc)


async def test_processor_validate_accepts_safe_public_url(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "http://93.184.216.34/video")

    # Must not raise.
    await DownloadersYoutubeProcessor().validate(job=None, file_doc=file_doc)
