"""Tests for the Grammar Checker Tier 1 endpoint (`POST /v1/ai_tools/
grammar_checker`), backed by `app.services.ai.grammar_checker.check_grammar`
(the public LanguageTool API via `aiohttp`).

Covers:
  1. 200 happy path - a mocked LanguageTool response with `matches[]` maps to
     the expected `correctedText`/`issues[]` shape.
  2. 400 path - empty text and text over `MAX_TEXT_LENGTH` (20,000 chars).
  3. 503 path - upstream timeout, connection error, HTTP 429, and HTTP 5xx all
     degrade to the same generic "unavailable" response, with the raw
     aiohttp/library exception text/type never appearing anywhere in the
     response body (the same leak class `grammar.py`/`keyword_extract.py`
     were fixed for - see `tests/test_seo_tools_robustness.py`).
  4. 500 path - a genuinely unexpected failure still gets the generic,
     non-leaking `GRAMMAR_CHECK_FAILED` envelope (mirrors
     `tests/test_sentiment_consolidation.py`'s equivalent regression test).
  5. Service-level, no-HTTP coverage of `_apply_corrections`'s offset
     bookkeeping, including a multi-match, overlapping-adjacent case, to
     prove replacements applied right-to-left don't corrupt earlier offsets.

Deliberately does NOT reuse `tests/conftest.py`'s `test_app`/`client`
fixtures for the HTTP-level tests: those provision an ARQ Redis pool via
`app.state.arq_redis`, and local Redis isn't reachable in this environment.
`ai_tools` never touches `app.state.arq_redis` at all, so a lighter,
Redis-free test app - mounting only `ai_tools` - is enough, mirroring
`build_sentiment_test_app()` in `tests/test_sentiment_consolidation.py`
(same StarletteHTTPException/RequestValidationError/Exception handlers,
same real-local-Mongo-backed API-key fixture).

`aiohttp.ClientSession` is faked out the same way
`tests/test_web_tools_whois_ip_speed.py`/`tests/test_web_tools_uptime_dns_ssl.py`
fake it for `check_url()`/`speed_test()`: a scripted fake response/session/
session-factory patched onto `grammar_checker_service.aiohttp.ClientSession`
via `monkeypatch`, no real network I/O.
"""
import asyncio
import os

# Must happen before any `app.*` import, same as tests/conftest.py.
os.environ.setdefault("DATABASE_URL", "mongodb://localhost:27017")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379")

import aiohttp
import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.services.ai.grammar_checker as grammar_checker_service
from app.routers import ai_tools as ai_tools_router
from app.services.ai.grammar_checker import _apply_corrections
from tests.conftest import _cleanup_api_key, _make_api_key

asyncio_session = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


# --- test app / fixtures ----------------------------------------------------


def build_grammar_test_app() -> FastAPI:
    """Mounts only `ai_tools` - no `files`/`jobs`/`pdf`/`image`, no
    `app.state.arq_redis` pool. Mirrors `build_sentiment_test_app()` in
    `tests/test_sentiment_consolidation.py`."""
    app = FastAPI()
    app.include_router(ai_tools_router.router, prefix="/v1")

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
async def grammar_client():
    app = build_grammar_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def api_key():
    """A key with access to the `ai_tools` category - same shape as
    `tests/conftest.py`'s own `api_key` fixture (category "all"), duplicated
    locally so this module has no dependency on the Redis-touching fixtures
    in that file, only its plain Mongo helpers."""
    key = await _make_api_key(categories=["all"])
    yield key
    await _cleanup_api_key(key["id"])


# --- fake aiohttp plumbing (mirrors tests/test_web_tools_whois_ip_speed.py) --


class _FakeGrammarResponse:
    """Mimics just enough of `aiohttp.ClientResponse` for `check_grammar()`:
    `.status`, async `.json()`, and async-context-manager support (`async
    with session.post(...) as response:`)."""

    def __init__(self, status: int, payload: dict | None = None):
        self.status = status
        self._payload = payload or {}

    async def json(self):
        return self._payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _ScriptedGrammarSession:
    """Fake `aiohttp.ClientSession` instance: returns a single scripted
    `_FakeGrammarResponse` for `.post(...)`, or raises a scripted exception
    synchronously from `.post(...)` itself (same idiom as
    `_ScriptedSpeedSession.get`'s `raise_on_get`) to simulate a
    timeout/connection failure happening on the call."""

    def __init__(self, response=None, raise_on_post: Exception | None = None):
        self._response = response
        self._raise_on_post = raise_on_post
        self.calls: list[tuple[str, dict]] = []

    def post(self, url, data=None, **kwargs):
        self.calls.append((url, data))
        if self._raise_on_post is not None:
            raise self._raise_on_post
        return self._response

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _ScriptedGrammarSessionFactory:
    """Stands in for `aiohttp.ClientSession` itself (the class) -
    `check_grammar()` does `async with aiohttp.ClientSession(timeout=...) as
    session:`, so this needs to be both callable (the constructor call,
    accepting/ignoring the `timeout` kwarg) and an async context manager
    yielding the pre-built scripted session."""

    def __init__(self, session):
        self._session = session

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


def _patch_grammar_session(monkeypatch, session):
    monkeypatch.setattr(
        grammar_checker_service.aiohttp, "ClientSession", _ScriptedGrammarSessionFactory(session)
    )


_LANGUAGETOOL_PAYLOAD = {
    "matches": [
        {
            "message": "Possible spelling mistake found.",
            "offset": 0,
            "length": 4,
            "rule": {"id": "MORFOLOGIK_RULE_EN_US", "issueType": "misspelling", "category": {"id": "TYPOS"}},
            "replacements": [{"value": "This"}, {"value": "Thus"}],
        }
    ]
}


# --- 200 happy path -----------------------------------------------------


@asyncio_session
async def test_grammar_checker_valid_text_returns_200_with_corrections(
    grammar_client, api_key, monkeypatch
):
    session = _ScriptedGrammarSession(response=_FakeGrammarResponse(200, _LANGUAGETOOL_PAYLOAD))
    _patch_grammar_session(monkeypatch, session)

    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "Thsi is a test sentence."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["text"] == "Thsi is a test sentence."
    assert body["language"] == "en-US"
    assert body["correctedText"] == "This is a test sentence."
    assert len(body["issues"]) == 1
    issue = body["issues"][0]
    assert issue["ruleId"] == "MORFOLOGIK_RULE_EN_US"
    assert issue["type"] == "misspelling"
    assert issue["offset"] == 0
    assert issue["length"] == 4
    assert issue["replacements"] == ["This", "Thus"]

    # The upstream call used the caller's text/language, unmodified.
    assert session.calls == [
        ("https://api.languagetool.org/v2/check", {"text": "Thsi is a test sentence.", "language": "en-US"})
    ]


@asyncio_session
async def test_grammar_checker_no_issues_returns_200_with_unmodified_text(
    grammar_client, api_key, monkeypatch
):
    session = _ScriptedGrammarSession(response=_FakeGrammarResponse(200, {"matches": []}))
    _patch_grammar_session(monkeypatch, session)

    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "This sentence is already correct."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["correctedText"] == "This sentence is already correct."
    assert body["issues"] == []


@asyncio_session
async def test_grammar_checker_custom_language_is_passed_through(
    grammar_client, api_key, monkeypatch
):
    session = _ScriptedGrammarSession(response=_FakeGrammarResponse(200, {"matches": []}))
    _patch_grammar_session(monkeypatch, session)

    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "Ceci est un test.", "language": "fr"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["language"] == "fr"
    assert session.calls == [
        ("https://api.languagetool.org/v2/check", {"text": "Ceci est un test.", "language": "fr"})
    ]


# --- 400 path: input validation ---------------------------------------------


@asyncio_session
async def test_grammar_checker_empty_text_returns_400(grammar_client, api_key):
    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert "cannot be empty" in body["message"]
    assert body["error"]["code"] == "GRAMMAR_CHECK_INVALID_INPUT"


@asyncio_session
async def test_grammar_checker_whitespace_only_text_returns_400(grammar_client, api_key):
    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "   \n\t  "},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == "GRAMMAR_CHECK_INVALID_INPUT"


@asyncio_session
async def test_grammar_checker_wildly_oversized_body_rejected_by_schema(grammar_client, api_key):
    """A body well beyond even the generous Field(max_length=...) outer
    ceiling is rejected by Pydantic (422) rather than being fully parsed and
    handed to the service layer - defense-in-depth against an unbounded
    body being parsed into memory before any business-rule check runs."""
    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "a" * 200_000},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


@asyncio_session
async def test_grammar_checker_text_over_max_length_returns_400(grammar_client, api_key):
    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "a" * 20_001},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert "at most 20000 characters" in body["message"] or "20000" in body["message"]
    assert body["error"]["code"] == "GRAMMAR_CHECK_INVALID_INPUT"


@asyncio_session
async def test_grammar_checker_text_at_max_length_is_not_rejected(
    grammar_client, api_key, monkeypatch
):
    """Boundary check: exactly `MAX_TEXT_LENGTH` chars must NOT be treated as
    invalid input (only strictly over the limit is)."""
    session = _ScriptedGrammarSession(response=_FakeGrammarResponse(200, {"matches": []}))
    _patch_grammar_session(monkeypatch, session)

    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "a" * 20_000},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text


# --- 503 path: upstream failures, no leaked exception detail ----------------


@asyncio_session
async def test_grammar_checker_upstream_timeout_returns_503_without_leaking_exception_text(
    grammar_client, api_key, monkeypatch
):
    session = _ScriptedGrammarSession(raise_on_post=asyncio.TimeoutError(_SECRET_MARKER))
    _patch_grammar_session(monkeypatch, session)

    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "Some text to check."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "GRAMMAR_CHECK_UNAVAILABLE"
    assert "temporarily unavailable" in body["message"]
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "TimeoutError" not in resp.text


@asyncio_session
async def test_grammar_checker_connection_error_returns_503_without_leaking_exception_text(
    grammar_client, api_key, monkeypatch
):
    session = _ScriptedGrammarSession(raise_on_post=aiohttp.ClientOSError(_SECRET_MARKER))
    _patch_grammar_session(monkeypatch, session)

    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "Some text to check."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "GRAMMAR_CHECK_UNAVAILABLE"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "ClientOSError" not in resp.text


@asyncio_session
async def test_grammar_checker_upstream_rate_limited_returns_503(
    grammar_client, api_key, monkeypatch
):
    session = _ScriptedGrammarSession(response=_FakeGrammarResponse(429))
    _patch_grammar_session(monkeypatch, session)

    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "Some text to check."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"]["code"] == "GRAMMAR_CHECK_UNAVAILABLE"
    assert "429" not in resp.text


@asyncio_session
async def test_grammar_checker_upstream_5xx_returns_503(grammar_client, api_key, monkeypatch):
    session = _ScriptedGrammarSession(response=_FakeGrammarResponse(502))
    _patch_grammar_session(monkeypatch, session)

    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "Some text to check."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 503, resp.text
    body = resp.json()
    assert body["error"]["code"] == "GRAMMAR_CHECK_UNAVAILABLE"
    assert "502" not in resp.text


# --- 500 path: genuinely unexpected failure, still no leak -------------------


class _BoomCheckGrammar:
    """Stands in for `check_grammar` and raises an unrelated, unexpected
    exception (not `ValueError`/`GrammarCheckerUnavailableError`), forcing
    the router's generic `except Exception` branch."""

    async def __call__(self, *args, **kwargs):
        raise RuntimeError(f"{_SECRET_MARKER} at /app/services/ai/grammar_checker.py")


@asyncio_session
async def test_grammar_checker_unexpected_internal_failure_returns_500_without_leaking_exception_text(
    grammar_client, api_key, monkeypatch
):
    monkeypatch.setattr(ai_tools_router, "check_grammar", _BoomCheckGrammar())
    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "Some text to check."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to check grammar"
    assert body["error"]["code"] == "GRAMMAR_CHECK_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "grammar_checker.py" not in resp.text


# --- auth ---------------------------------------------------------------


@asyncio_session
async def test_grammar_checker_rejects_unauthorized_request(grammar_client):
    resp = await grammar_client.post(
        "/v1/ai_tools/grammar_checker",
        json={"text": "Some text to check."},
    )
    assert resp.status_code == 422  # missing required X-API-Key header


# --- service-level: _apply_corrections offset bookkeeping --------------------


def test_apply_corrections_single_match_replaces_in_place():
    text = "Thsi is fine."
    matches = [{"offset": 0, "length": 4, "replacements": [{"value": "This"}]}]
    assert _apply_corrections(text, matches) == "This is fine."


def test_apply_corrections_multiple_overlapping_adjacent_matches_offsets_not_corrupted():
    """Three matches back-to-back with no gaps between them, and each
    replacement a different length than the original span (so a naive
    left-to-right application would shift every later offset). Applying
    right-to-left by offset must still produce the fully-corrected text."""
    text = "Teh qick brown fox jmps ofer teh lasy dog"
    #        0         1         2         3
    #        0123456789012345678901234567890123456789
    # "Teh"  -> offset 0,  length 3 -> "The"
    # "qick" -> offset 4,  length 4 -> "quick"   (longer: +1 shift)
    # "jmps" -> offset 19, length 4 -> "jumps"   (longer: +1 shift)
    matches = [
        # Deliberately out of offset order, to prove sorting (not input
        # order) drives the right-to-left application.
        {"offset": 19, "length": 4, "replacements": [{"value": "jumps"}]},
        {"offset": 0, "length": 3, "replacements": [{"value": "The"}]},
        {"offset": 4, "length": 4, "replacements": [{"value": "quick"}]},
    ]
    corrected = _apply_corrections(text, matches)
    assert corrected == "The quick brown fox jumps ofer teh lasy dog"


def test_apply_corrections_adjacent_matches_with_no_gap_between_spans():
    """Two matches whose spans are directly adjacent (no untouched text
    between them) - the classic "overlapping-adjacent" offset-corruption
    trap: applying the earlier (lower-offset) match first would shift the
    later match's offset out from under it."""
    text = "aabbcc"
    matches = [
        {"offset": 0, "length": 2, "replacements": [{"value": "XXX"}]},  # "aa" -> "XXX"
        {"offset": 2, "length": 2, "replacements": [{"value": "Y"}]},  # "bb" -> "Y"
    ]
    corrected = _apply_corrections(text, matches)
    assert corrected == "XXXYcc"


def test_apply_corrections_skips_match_with_no_replacement_suggestions():
    text = "Some text with an issue."
    matches = [
        {"offset": 5, "length": 4, "replacements": []},
        {"offset": 0, "length": 4, "replacements": [{"value": "Any"}]},
    ]
    corrected = _apply_corrections(text, matches)
    # The offset=5 match is left untouched (no suggestion to apply); the
    # offset=0 match is still applied correctly.
    assert corrected == "Any text with an issue."


def test_apply_corrections_no_matches_returns_text_unchanged():
    text = "Nothing wrong here."
    assert _apply_corrections(text, []) == text


def test_apply_corrections_genuinely_overlapping_spans_skips_the_earlier_one():
    """Two matches whose spans truly overlap (not just adjacent) - e.g. two
    different rules flagging intersecting parts of the same passage. The
    later-applied (further-right) match wins; the earlier, now-conflicting
    match is skipped rather than corrupting already-rewritten text."""
    text = "abcdef"
    matches = [
        {"offset": 0, "length": 4, "replacements": [{"value": "ZZZZ"}]},  # "abcd", overlaps below
        {"offset": 2, "length": 2, "replacements": [{"value": "Y"}]},  # "cd", offset 2-4
    ]
    corrected = _apply_corrections(text, matches)
    # offset=2 applied first (rightmost), consumed_from becomes 2; offset=0
    # match's span (0-4) exceeds that boundary, so it's skipped untouched.
    assert corrected == "abYef"


def test_apply_corrections_identical_offset_duplicate_matches_applies_only_one():
    text = "abcdef"
    matches = [
        {"offset": 2, "length": 2, "replacements": [{"value": "X"}]},
        {"offset": 2, "length": 2, "replacements": [{"value": "Y"}]},
    ]
    corrected = _apply_corrections(text, matches)
    assert corrected in ("abXef", "abYef")
    # Exactly one of the two duplicate-offset matches was applied, not both
    # (which would have doubled up or corrupted the span).
    assert len(corrected) == len(text) - 2 + 1
