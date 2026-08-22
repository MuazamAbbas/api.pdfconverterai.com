"""Tests for the TextBlob sentiment consolidation (ADR-015 Open Item 1).

Covers:
  1. `POST /v1/ai_tools/sentiment` (the sole surviving implementation, backed
     by `app.services.ai_tools.sentiment.analyze_sentiment_service`) still
     behaves exactly as before the consolidation: positive/negative/neutral
     text -> 200 with the expected `text`/`polarity`/`subjectivity`/`label`
     fields, too-short text -> 400, empty text -> 400.
  2. `app.routers.text.router` no longer exposes a `/sentiment` route at all
     (neither in the route table nor over the wire).
  3. The rest of `app.routers.text` still imports and works (a `/word_count`
     smoke check) - i.e. removing the `SentimentAnalyzer`/`SentimentResponse`
     imports didn't break anything else in that module.

Deliberately does NOT reuse `tests/conftest.py`'s `test_app`/`client`
fixtures: those provision an ARQ Redis pool via `app.state.arq_redis`
(`create_pool(...)` in the `test_app` fixture), and local Redis isn't
reachable in this environment. Sentiment/word-count/char-count etc. never
touch `app.state.arq_redis` at all (only `/text/paraphrase`, `/text/summarize`
and `/text/upload` do, and none of those are exercised here), so a lighter,
Redis-free test app - mounting only `ai_tools` and `text` - is enough and
avoids that dependency entirely. Mirrors `build_test_app()` in
`tests/conftest.py` (same StarletteHTTPException/RequestValidationError/
Exception handlers) minus the Redis pool.

Real local MongoDB (`mongodb://localhost:27017`, db `pdfconverterai`) is
used for the API-key fixture, same as every other test module in this
suite - `app.core.security.verify_api_key` opens its own Motor client
against `settings.database_url` on every call, so there's no way to fake
this out without also faking Mongo.

NOTE on router mounting: `app.routers.ai_tools`'s `router` used to be defined
with `APIRouter(prefix="/v1/ai_tools", ...)` - the only router in
`app/routers/` that baked `/v1` into its own prefix, while `app/main.py`
separately did `app.include_router(ai_tools.router, prefix="/v1", ...)` on
top of that, double-prefixing it to `/v1/v1/ai_tools/...` in the real running
app. Fixed (ADR-015 Open Item 3): `ai_tools.router` now declares
`prefix="/ai_tools"`, matching every sibling router's bare-resource-prefix
convention (e.g. `text.router`'s `prefix="/text"`), and relies on the `/v1`
mount prefix in `app/main.py` like everything else. This test file mounts
`ai_tools.router` with an explicit `prefix="/v1"` (mirroring `app/main.py`)
so it lands on `/v1/ai_tools/...`.
"""
import os

# Must happen before any `app.*` import, same as tests/conftest.py - though
# by the time pytest gets here conftest.py has already set these, this keeps
# the module independently runnable/importable.
os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.services.ai_tools.sentiment as sentiment_service
from app.routers import ai_tools as ai_tools_router
from app.routers import text as text_router
from tests.conftest import _cleanup_api_key, _make_api_key

# Applied per-async-test (not as a blanket module-level `pytestmark`) since
# this module also has one plain sync test
# (`test_text_router_has_no_sentiment_route`) that pytest-asyncio would
# otherwise warn about being marked `asyncio` unnecessarily.
asyncio_session = pytest.mark.asyncio(loop_scope="session")


def build_sentiment_test_app() -> FastAPI:
    """Mounts only `ai_tools` (sentiment) and `text` - no `files`/`jobs`/`pdf`/
    `image`, no `app.state.arq_redis` pool. Mounts both routers the same way
    `app/main.py` does (explicit `prefix="/v1"` on top of each router's own
    bare resource prefix) now that `ai_tools.router` no longer bakes `/v1`
    into its own prefix - see module docstring.
    """
    app = FastAPI()
    app.include_router(ai_tools_router.router, prefix="/v1")
    app.include_router(text_router.router, prefix="/v1")

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
async def sentiment_client():
    app = build_sentiment_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def api_key():
    """A key with access to both the `ai_tools` and `text` categories - same
    shape as `tests/conftest.py`'s own `api_key` fixture (category "all"),
    duplicated locally so this module has no dependency on the Redis-touching
    fixtures in that file, only its plain Mongo helpers."""
    key = await _make_api_key(categories=["all"])
    yield key
    await _cleanup_api_key(key["id"])


# --- POST /v1/ai_tools/sentiment (surviving implementation) -----------------


@asyncio_session
async def test_sentiment_positive_text_returns_200(sentiment_client, api_key):
    text = "I absolutely love this wonderful product, it is amazing!"
    resp = await sentiment_client.post(
        "/v1/ai_tools/sentiment",
        json={"text": text},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == text
    assert body["label"] == "positive"
    assert body["polarity"] > 0
    assert 0.0 <= body["subjectivity"] <= 1.0


@asyncio_session
async def test_sentiment_negative_text_returns_200(sentiment_client, api_key):
    text = "This is a terrible awful experience, I hate it so much."
    resp = await sentiment_client.post(
        "/v1/ai_tools/sentiment",
        json={"text": text},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == text
    assert body["label"] == "negative"
    assert body["polarity"] < 0


@asyncio_session
async def test_sentiment_neutral_text_returns_200(sentiment_client, api_key):
    text = "The meeting is scheduled for Tuesday at noon."
    resp = await sentiment_client.post(
        "/v1/ai_tools/sentiment",
        json={"text": text},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == text
    assert body["label"] == "neutral"
    assert body["polarity"] == 0


@asyncio_session
async def test_sentiment_too_short_text_returns_400(sentiment_client, api_key):
    resp = await sentiment_client.post(
        "/v1/ai_tools/sentiment",
        json={"text": "Hi"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    assert "at least 10 characters" in resp.json()["message"]


@asyncio_session
async def test_sentiment_empty_text_returns_400(sentiment_client, api_key):
    resp = await sentiment_client.post(
        "/v1/ai_tools/sentiment",
        json={"text": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    assert "cannot be empty" in resp.json()["message"]


_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


class _BoomTextBlob:
    """Stands in for `textblob.TextBlob` and raises on construction, so
    `analyze_sentiment_service`'s `try` block sees a genuine unexpected
    failure (not one of its own intentional `ValueError`s for empty/short
    text)."""

    def __init__(self, *args, **kwargs):
        raise RuntimeError(f"{_SECRET_MARKER} at /app/services/ai_tools/sentiment.py")


@pytest.mark.asyncio(loop_scope="session")
async def test_analyze_sentiment_service_reraises_original_exception_type():
    """Service-level regression test for the Batch 2 fix: an unexpected
    failure inside `analyze_sentiment_service`'s `try` block must propagate
    as its own real exception type (here `RuntimeError`), not get wrapped
    into a `ValueError` the way it used to (the same leak pattern Batch 1
    fixed in `grammar.py`'s `correct_grammar`)."""
    import app.services.ai_tools.sentiment as svc

    original_textblob = svc.TextBlob
    svc.TextBlob = _BoomTextBlob
    try:
        with pytest.raises(RuntimeError) as excinfo:
            await svc.analyze_sentiment_service(
                "This is long enough text to pass length validation."
            )
        assert _SECRET_MARKER in str(excinfo.value)
        assert not isinstance(excinfo.value, ValueError)
    finally:
        svc.TextBlob = original_textblob


@asyncio_session
async def test_sentiment_unexpected_internal_failure_returns_500_without_leaking_exception_text(
    sentiment_client, api_key, monkeypatch
):
    """Router-level regression test for the Batch 2 fix: the same forced
    failure, now driven all the way through `POST /v1/ai_tools/sentiment`,
    must land in the router's generic `except Exception` branch and return
    the safe, generic `api_error` envelope - no raw exception text, class
    name, or file path anywhere in the response body."""
    monkeypatch.setattr(sentiment_service, "TextBlob", _BoomTextBlob)
    resp = await sentiment_client.post(
        "/v1/ai_tools/sentiment",
        json={"text": "This is long enough text to pass length validation."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to analyze sentiment"
    assert body["error"]["code"] == "SENTIMENT_ANALYSIS_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "sentiment.py" not in resp.text


@asyncio_session
async def test_sentiment_rejects_unauthorized_request(sentiment_client):
    """No `X-API-Key` header at all -> the endpoint is still protected the
    same way every other route in this router-group is (unlike the real
    `app/main.py`, this test app doesn't apply `verify_api_key` as a
    router-level `dependencies=[...]`, but `ai_tools.py` also takes it as a
    per-route `Depends`, so it's still enforced)."""
    resp = await sentiment_client.post(
        "/v1/ai_tools/sentiment",
        json={"text": "Whatever text, long enough to pass length validation."},
    )
    assert resp.status_code == 422  # missing required X-API-Key header


# --- app.routers.text no longer has a /sentiment route ----------------------


def test_text_router_has_no_sentiment_route():
    paths = [r.path for r in text_router.router.routes]
    assert "/text/sentiment" not in paths
    assert not any(p.endswith("/sentiment") for p in paths)


@asyncio_session
async def test_text_sentiment_endpoint_returns_404(sentiment_client, api_key):
    resp = await sentiment_client.post(
        "/v1/text/sentiment",
        json={"text": "Whatever text, long enough to pass length validation."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404


# --- app.routers.text smoke check (import + one working endpoint) -----------


@asyncio_session
async def test_text_word_count_still_works(sentiment_client, api_key):
    text = "one two three four five"
    resp = await sentiment_client.post(
        "/v1/text/word_count",
        json={"text": text},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"]["word_count"] == 5
