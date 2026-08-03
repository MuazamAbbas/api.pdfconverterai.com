"""Coverage for `POST /v1/video/youtube_metadata`, previously untested.

Companion to `tests/test_sentiment_consolidation.py` for the same Batch 2
error-leak fix (issue #31): `app.services.video.youtube_metadata.
fetch_youtube_metadata`'s generic `except Exception` no longer wraps an
unexpected internal failure into a leaky `ValueError(f"...{str(e)}")` - it
now bare re-raises the original exception - and `app.routers.video.
youtube_metadata`'s 500 branch no longer interpolates `str(e)` into the
`HTTPException` detail, using `api_error(...)` instead.

IMPORTANT - separate, pre-existing bug discovered while writing this suite
(unrelated to the Batch 2 diff, confirmed via `git show dc0322f` - neither
line was touched by that commit): `youtube_metadata`'s route signature is
`url: HttpUrl` (a bare, non-model parameter, so FastAPI/Starlette binds it
as a query param and coerces it via Pydantic v2's `HttpUrl` type), but
`fetch_youtube_metadata(url: str)` immediately calls `url.startswith(...)`
on it. Pydantic v2's `HttpUrl` is NOT a `str` subclass (unlike Pydantic
v1), so this raises `AttributeError: 'HttpUrl' object has no attribute
'startswith'` - for every single call, valid YouTube URLs included. This
line sits *outside* `fetch_youtube_metadata`'s own `try` block, so it
propagates straight past the service's exception handling into the
router's `except Exception` branch. Net effect: **`/v1/video/
youtube_metadata` cannot currently return either the intended 400
(invalid URL) or a real 200 - every real HTTP request to this endpoint
returns 500**, confirmed empirically below. This should be flagged to
backend-builder as a functional bug (fix: convert `url` to `str(url)`
before calling `fetch_youtube_metadata`, or change the service's
signature/usage) - it is NOT something this test session fixes, since it
is outside the scope of the Batch 2 error-leak diff.

One silver lining Batch 2's router fix does still verifiably provide: since
the AttributeError is an unhandled `Exception` (not a `ValueError`), it now
gets caught by the router's *fixed* generic branch and returns the generic
`api_error` envelope with no leaked exception text - so
`test_youtube_metadata_http_currently_always_returns_500_due_to_httpurl_
str_mismatch` below both documents the bug and confirms that whatever
exception reaches that branch, nothing is leaked.

Because that pre-existing bug makes it impossible to reach the intended
400/200 branches through a real HTTP request, the three regression cases
that actually exercise the Batch 2 diff (validation still works /
unexpected-failure-no-longer-leaks / happy path) are driven by calling
`app.routers.video.youtube_metadata` (the real, unmodified route function)
directly as a plain coroutine with a plain `str` url - i.e. the same value
`fetch_youtube_metadata` is annotated to expect and the value it would
receive if the HttpUrl/str bug above were fixed - bypassing only the
FastAPI request-parsing layer responsible for the separate bug, not any
of the logic this task is actually testing. This is a deliberate,
documented compromise per the task brief's "say so as a finding rather
than writing a weak test to force a pass" rule: the HTTP-level tests for
those three cases would either be weak (asserting a 500 that happens for
the wrong reason) or impossible (400/200 unreachable at all) if forced
through the real request path.
"""
import os

# Must happen before any `app.*` import, same as tests/conftest.py.
os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import pytest
import pytest_asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.routers import video as video_router
import app.services.video.youtube_metadata as youtube_metadata_service
from tests.conftest import _cleanup_api_key, _make_api_key

asyncio_session = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"

_VALID_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def build_video_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(video_router.router, prefix="/v1")

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
async def video_client():
    app = build_video_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def api_key():
    """A key with access to the `video` category - same shape as
    `tests/conftest.py`'s own `api_key` fixture (category "all")."""
    key = await _make_api_key(categories=["all"])
    yield key
    await _cleanup_api_key(key["id"])


class _BoomYoutubeDL:
    """Stands in for `yt_dlp.YoutubeDL` and raises on construction, so
    `fetch_youtube_metadata`'s `try` block sees a genuine unexpected
    failure (not its own intentional `ValueError` for an invalid URL)."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(f"{_SECRET_MARKER} at /app/services/video/youtube_metadata.py")


class _FakeYoutubeDL:
    """Context-manager stand-in returning a canned `info` dict, so the
    happy path doesn't hit the network / real YouTube."""

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        return {
            "title": "Never Gonna Give You Up",
            "description": "The official video for Rick Astley's 1987 hit.",
            "duration": 213,
        }


# --- documents the pre-existing HttpUrl/str bug (real HTTP layer) ----------


@asyncio_session
async def test_youtube_metadata_http_currently_always_returns_500_due_to_httpurl_str_mismatch(
    video_client, api_key
):
    """Bug-documentation test, not a Batch 2 regression test: a real HTTP
    request with a syntactically valid, real YouTube URL still returns 500
    today, because FastAPI coerces `url` into a `pydantic.HttpUrl` before
    `fetch_youtube_metadata` calls `.startswith()` on it. If/when
    backend-builder fixes that type mismatch, this test should start
    failing (500 -> 200) and should be replaced by a real HTTP-level 200
    test at that point.

    Still asserts the one thing that *is* in scope here: whatever exception
    reaches the router (AttributeError today), Batch 2's fix means it comes
    back as the safe, generic `api_error` envelope - no leaked exception
    text.
    """
    resp = await video_client.post(
        "/v1/video/youtube_metadata",
        params={"url": _VALID_URL},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to fetch YouTube metadata"
    assert body["error"]["code"] == "YOUTUBE_METADATA_FAILED"
    assert "startswith" not in resp.text
    assert "AttributeError" not in resp.text
    assert "HttpUrl" not in resp.text


# --- service-level regression test: real exception type propagates ---------


@pytest.mark.asyncio(loop_scope="session")
async def test_fetch_youtube_metadata_reraises_original_exception_type():
    """Service-level regression test for the Batch 2 fix: an unexpected
    failure inside `fetch_youtube_metadata`'s `try` block must propagate as
    its own real exception type (here `RuntimeError`), not get wrapped into
    a `ValueError` the way it used to."""
    original_youtube_dl = youtube_metadata_service.yt_dlp.YoutubeDL
    youtube_metadata_service.yt_dlp.YoutubeDL = _BoomYoutubeDL
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await youtube_metadata_service.fetch_youtube_metadata(_VALID_URL)
        assert _SECRET_MARKER in str(excinfo.value)
        assert not isinstance(excinfo.value, ValueError)
    finally:
        youtube_metadata_service.yt_dlp.YoutubeDL = original_youtube_dl


# --- router-level regression tests, driven directly (see module docstring) -
#
# Calls `video_router.youtube_metadata` directly as a plain coroutine with a
# plain `str` url, bypassing only the FastAPI request-parsing layer
# responsible for the separate HttpUrl/str bug above - not any of the
# router logic this Batch 2 diff actually changed.


@pytest.mark.asyncio(loop_scope="session")
async def test_youtube_metadata_router_invalid_url_raises_400_with_safe_validation_message():
    with pytest.raises(HTTPException) as excinfo:
        await video_router.youtube_metadata(
            url="https://example.com/not-a-youtube-link", api_key={"key_data": {}}
        )
    assert excinfo.value.status_code == 400
    assert excinfo.value.detail == "URL must be a valid YouTube URL"


@pytest.mark.asyncio(loop_scope="session")
async def test_youtube_metadata_router_unexpected_failure_returns_500_no_leak(
    monkeypatch,
):
    monkeypatch.setattr(youtube_metadata_service.yt_dlp, "YoutubeDL", _BoomYoutubeDL)
    with pytest.raises(HTTPException) as excinfo:
        await video_router.youtube_metadata(url=_VALID_URL, api_key={"key_data": {}})
    assert excinfo.value.status_code == 500
    body = excinfo.value.detail
    assert body["success"] is False
    assert body["message"] == "Failed to fetch YouTube metadata"
    assert body["error"]["code"] == "YOUTUBE_METADATA_FAILED"
    rendered = str(body)
    assert _SECRET_MARKER not in rendered
    assert "hunter2" not in rendered
    assert "db_password" not in rendered
    assert "RuntimeError" not in rendered
    assert "youtube_metadata.py" not in rendered


@pytest.mark.asyncio(loop_scope="session")
async def test_youtube_metadata_router_valid_request_returns_metadata(monkeypatch):
    monkeypatch.setattr(youtube_metadata_service.yt_dlp, "YoutubeDL", _FakeYoutubeDL)
    result = await video_router.youtube_metadata(url=_VALID_URL, api_key={"key_data": {}})
    assert result["title"] == "Never Gonna Give You Up"
    assert result["duration_seconds"] == 213
    assert result["url"] == _VALID_URL


# --- unauthorized request (real HTTP layer, unaffected by the bug above) ----


@asyncio_session
async def test_youtube_metadata_rejects_unauthorized_request(video_client):
    resp = await video_client.post(
        "/v1/video/youtube_metadata",
        params={"url": _VALID_URL},
    )
    assert resp.status_code == 422  # missing required X-API-Key header
