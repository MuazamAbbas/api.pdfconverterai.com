"""Unit + HTTP tests for the new `auth` module (human-admin login gate,
founder-approved architecture decisions, ADR-019 pending).

Covers: password hashing round trip, JWT issuance/verification (including
expiry + wrong-type/tampered-signature rejection), `require_admin`'s
missing/invalid/valid-cookie branches, and the full `POST /v1/auth/login`
HTTP round trip (unknown email vs. wrong password return the identical 401
body, successful login sets an httpOnly/Secure/SameSite=Strict cookie and
never puts the token in the JSON body, and the lockout counter after
repeated failures). Uses the real local Mongo (`admin_users` collection),
per this suite's existing `tests/conftest.py` convention - never mocked.
"""
import logging
from datetime import datetime, timedelta

import pytest
import pytest_asyncio
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.admin_auth import ADMIN_COOKIE_NAME, require_admin
from app.core.config import settings
from app.core.database import db
from app.core.rate_limiter import limiter
from app.routers import auth as auth_router
from app.services.auth import admin_user_service
from app.services.auth.login_service import LoginResult, attempt_login
from app.services.auth.password_service import hash_password, verify_password
from app.services.auth.token_service import create_admin_access_token, decode_admin_access_token

# Pins every test in this module to the session-scoped event loop, matching
# tests/conftest.py's own `asyncio_default_fixture_loop_scope = "session"`
# reasoning (pyproject.toml): app.core.database.db's Motor client binds to
# whichever loop first uses it, so an async test running on a
# function-scoped loop instead breaks it ("attached to a different loop").
# Harmlessly also emits a PytestWarning on this file's handful of plain
# (non-async) unit tests - cosmetic only, doesn't fail the run.
pytestmark = pytest.mark.asyncio(loop_scope="session")

_TEST_EMAIL = "seed-test-admin@pdfconverterai.com"
_TEST_PASSWORD = "a-strong-test-password-123"


def _build_test_app() -> FastAPI:
    """Same shape as tests/conftest.py's build_test_app(), scoped to just
    the `auth` router - it needs `app.state.limiter` (unlike the other
    routers under test there) because POST /auth/login is rate-limited via
    the shared slowapi Limiter in app/core/rate_limiter.py."""
    app = FastAPI()
    app.state.limiter = limiter
    app.include_router(auth_router.router, prefix="/v1")

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


@pytest_asyncio.fixture
async def client():
    app = _build_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def seeded_admin():
    """Inserts one admin_users document directly (bypassing seed_admin.py's
    CLI), cleans it up afterward. Never touches any other document."""
    await db.admin_users.delete_one({"email": _TEST_EMAIL})
    admin = await admin_user_service.create_admin_user(
        email=_TEST_EMAIL, password_hash=hash_password(_TEST_PASSWORD)
    )
    yield admin
    await db.admin_users.delete_one({"_id": admin.id})


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Clears the shared `app.core.rate_limiter.limiter`'s in-memory hit
    counters before AND after every test in this module.

    `limiter` is a true process-wide singleton (imported by value into
    every FastAPI app instance this module builds - `_build_test_app()`'s
    apps and the real `app.main.app` referenced by
    `test_real_app_never_logs_plaintext_password_on_malformed_login`
    below), so without this reset, login attempts made by one test count
    against the same `10/minute` POST /auth/login budget as every other
    test in this module/run, making any test that asserts specific
    rate-limit behavior (see `test_login_rate_limit_returns_429_after_
    threshold`) flaky depending on run order/count (testability gap
    flagged in review). See app/core/rate_limiter.py's module docstring
    for the same note.
    """
    limiter.reset()
    yield
    limiter.reset()


# --- admin_user_service ------------------------------------------------
#
# Coverage gap found in review: `create_admin_user`'s docstring and
# `app/schemas/admin_user.py`'s own docstring both describe the
# find-before-insert check (plus the unique index as the real, race-safe
# guarantee) as the only thing standing between `scripts/seed_admin.py`
# and a silently-duplicated/overwritten admin account, but nothing
# exercised that rejection path before this test.


async def test_create_admin_user_rejects_duplicate_email(seeded_admin):
    with pytest.raises(ValueError):
        await admin_user_service.create_admin_user(
            email=_TEST_EMAIL, password_hash=hash_password("some-other-password")
        )
    # The original document must be untouched (not overwritten) by the
    # rejected second call.
    admin_doc = await db.admin_users.find_one({"email": _TEST_EMAIL})
    assert admin_doc["_id"] == seeded_admin.id


async def test_reset_admin_password_rejects_unknown_email():
    with pytest.raises(ValueError):
        await admin_user_service.reset_admin_password(
            email="no-such-admin-for-reset-test@pdfconverterai.com",
            password_hash=hash_password("irrelevant"),
        )


async def test_reset_admin_password_updates_hash_and_clears_lockout(seeded_admin):
    # Simulate a prior lockout that a stale value would otherwise silently
    # defeat the new password against (the exact bug reset_admin_password's
    # docstring says it prevents).
    await db.admin_users.update_one(
        {"_id": seeded_admin.id},
        {"$set": {"failed_login_attempts": 5, "locked_until": datetime.utcnow() + timedelta(minutes=15)}},
    )

    new_hash = hash_password("a-different-strong-password-456")
    updated = await admin_user_service.reset_admin_password(
        email=_TEST_EMAIL, password_hash=new_hash, operator="test-operator"
    )

    assert updated.password_hash == new_hash
    assert updated.failed_login_attempts == 0
    assert updated.locked_until is None

    admin_doc = await db.admin_users.find_one({"_id": seeded_admin.id})
    assert admin_doc["password_hash"] == new_hash
    assert admin_doc["failed_login_attempts"] == 0
    assert admin_doc["locked_until"] is None


# --- password_service -------------------------------------------------


def test_hash_password_round_trip():
    hashed = hash_password(_TEST_PASSWORD)
    assert hashed != _TEST_PASSWORD
    assert verify_password(_TEST_PASSWORD, hashed) is True
    assert verify_password("wrong-password", hashed) is False


def test_verify_password_rejects_malformed_hash():
    assert verify_password(_TEST_PASSWORD, "not-a-real-bcrypt-hash") is False


# --- token_service -------------------------------------------------


def test_create_and_decode_admin_access_token():
    token = create_admin_access_token(_TEST_EMAIL)
    payload = decode_admin_access_token(token)
    assert payload is not None
    assert payload.sub == _TEST_EMAIL


def test_decode_admin_access_token_rejects_bad_signature():
    token = create_admin_access_token(_TEST_EMAIL)
    # Flip a character in the middle of the signature segment rather than
    # the very last character - base64's padding bits mean some
    # last-character substitutions decode to the same bytes, which would
    # make this assertion flaky (a real risk, not just a test-only quirk:
    # it's exactly why `decode_admin_access_token` must rely on
    # `jose.jwt.decode`'s own signature check rather than any custom
    # byte-level parsing).
    mid = len(token) // 2
    flipped_char = "A" if token[mid] != "A" else "B"
    tampered = token[:mid] + flipped_char + token[mid + 1 :]
    assert decode_admin_access_token(tampered) is None


def test_decode_admin_access_token_rejects_expired():
    from jose import jwt

    expired_payload = {
        "sub": _TEST_EMAIL,
        "iat": datetime.utcnow() - timedelta(hours=20),
        "exp": datetime.utcnow() - timedelta(hours=1),
        "type": "admin_access",
    }
    expired_token = jwt.encode(
        expired_payload, settings.admin_jwt_secret, algorithm=settings.admin_jwt_algorithm
    )
    assert decode_admin_access_token(expired_token) is None


def test_decode_admin_access_token_rejects_wrong_type():
    from jose import jwt

    payload = {
        "sub": _TEST_EMAIL,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(hours=1),
        "type": "not-admin-access",
    }
    token = jwt.encode(payload, settings.admin_jwt_secret, algorithm=settings.admin_jwt_algorithm)
    assert decode_admin_access_token(token) is None


# --- require_admin -------------------------------------------------


class _FakeRequest:
    def __init__(self, cookies: dict):
        self.cookies = cookies


async def test_require_admin_missing_cookie_raises_401():
    with pytest.raises(Exception) as exc_info:
        await require_admin(_FakeRequest(cookies={}))
    assert exc_info.value.status_code == 401


async def test_require_admin_invalid_cookie_raises_401():
    with pytest.raises(Exception) as exc_info:
        await require_admin(_FakeRequest(cookies={ADMIN_COOKIE_NAME: "garbage"}))
    assert exc_info.value.status_code == 401


async def test_require_admin_valid_cookie_succeeds():
    token = create_admin_access_token(_TEST_EMAIL)
    result = await require_admin(_FakeRequest(cookies={ADMIN_COOKIE_NAME: token}))
    assert result == {"email": _TEST_EMAIL}


# --- login_service: plaintext password must never be logged ---------
#
# Coverage gap found in review: every docstring in app/services/auth/*
# claims a plaintext password is never logged, but nothing actually
# asserted that against the real logging output - a future refactor could
# silently add it to a log format string (e.g. an f-string debug line)
# without any test catching it. Exercises both branches of `attempt_login`
# that run when the credential is wrong (unknown email -> the constant-time
# dummy-hash path; known email/wrong password -> the real verify +
# register_failed_login path) with one distinctive, never-reused password
# value and asserts it's absent from every captured log record across all
# loggers (root propagation), not just a specific one.
#
# Deliberately calls `attempt_login` directly (service layer) rather than
# going through `POST /v1/auth/login` - keeps this off the shared
# `app.core.rate_limiter.limiter` budget entirely (see the HTTP round-trip
# tests below, which already use the full 10/minute allowance across this
# module) and needs no `client`/`test_app` fixture (no Redis dependency).


async def test_login_never_logs_plaintext_password(seeded_admin, caplog):
    distinctive_password = "S3cr3t-Plaintext-Marker-Should-Never-Appear-In-Any-Log"
    caplog.set_level(logging.DEBUG)

    unknown_result, _ = await attempt_login(
        "no-such-admin-for-log-test@pdfconverterai.com", distinctive_password
    )
    wrong_result, _ = await attempt_login(_TEST_EMAIL, distinctive_password)

    assert unknown_result == LoginResult.DENIED
    assert wrong_result == LoginResult.DENIED
    assert len(caplog.records) > 0  # sanity: something was actually logged
    for record in caplog.records:
        assert distinctive_password not in record.getMessage()
        assert distinctive_password not in str(record.args or "")


# --- POST /v1/auth/login + /v1/auth/logout (HTTP round trip) -------


async def test_login_success_sets_cookie_and_never_returns_token_in_body(client, seeded_admin):
    resp = await client.post(
        "/v1/auth/login", json={"email": _TEST_EMAIL, "password": _TEST_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    # The token must never appear anywhere in the JSON response body.
    assert "token" not in body
    assert "data" in body and (body["data"] is None or "token" not in body["data"])

    set_cookie = resp.headers.get("set-cookie", "")
    assert ADMIN_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie or "samesite=strict" in set_cookie.lower()


async def test_login_wrong_password_and_unknown_email_return_identical_401(client, seeded_admin):
    resp_wrong_password = await client.post(
        "/v1/auth/login", json={"email": _TEST_EMAIL, "password": "totally-wrong"}
    )
    resp_unknown_email = await client.post(
        "/v1/auth/login",
        json={"email": "no-such-admin@pdfconverterai.com", "password": "totally-wrong"},
    )

    assert resp_wrong_password.status_code == 401
    assert resp_unknown_email.status_code == 401
    assert resp_wrong_password.json() == resp_unknown_email.json()
    # No Set-Cookie on a failed login.
    assert "set-cookie" not in resp_wrong_password.headers
    assert "set-cookie" not in resp_unknown_email.headers


async def test_login_lockout_after_max_attempts(client, seeded_admin):
    for _ in range(settings.admin_login_max_attempts):
        resp = await client.post(
            "/v1/auth/login", json={"email": _TEST_EMAIL, "password": "totally-wrong"}
        )
        assert resp.status_code == 401

    # Now even the CORRECT password is refused, with the identical 401 body,
    # because the account is locked.
    locked_resp = await client.post(
        "/v1/auth/login", json={"email": _TEST_EMAIL, "password": _TEST_PASSWORD}
    )
    assert locked_resp.status_code == 401
    assert locked_resp.json()["message"] == "Invalid email or password"

    admin_doc = await db.admin_users.find_one({"email": _TEST_EMAIL})
    assert admin_doc["locked_until"] is not None


async def test_logout_clears_cookie(client):
    resp = await client.post("/v1/auth/logout")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert ADMIN_COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie


async def test_login_rejects_malformed_email():
    app = _build_test_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post("/v1/auth/login", json={"email": "not-an-email", "password": "x"})
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False


# --- rate limiting on POST /auth/login -------------------------------
#
# Testability gap flagged in review: without `_reset_rate_limiter` above,
# this test would be flaky depending on how many other login attempts ran
# earlier in this module (they collectively already exercise close to the
# `10/minute` budget). With the reset in place, this test - and any future
# one - can rely on always starting from a clean budget.


async def test_login_rate_limit_returns_429_after_threshold(client, seeded_admin):
    """Exercises `app/routers/auth.py`'s `@limiter.limit("10/minute")` on
    POST /auth/login end to end. The literal `10` below must stay in sync
    with that decorator's value.
    """
    _RATE_LIMIT = 10
    for _ in range(_RATE_LIMIT):
        resp = await client.post(
            "/v1/auth/login", json={"email": _TEST_EMAIL, "password": "totally-wrong"}
        )
        assert resp.status_code == 401

    limited_resp = await client.post(
        "/v1/auth/login", json={"email": _TEST_EMAIL, "password": "totally-wrong"}
    )
    assert limited_resp.status_code == 429


# --- regression test for the CRITICAL plaintext-password-in-log finding
#
# `test_login_never_logs_plaintext_password` above already covers
# `login_service.attempt_login`'s own log lines, but it calls that service
# function directly - it never goes through FastAPI/pydantic's request
# validation at all, so it could not have caught (and would not
# regression-guard) the actual bug: app/main.py's real, globally-shared
# `validation_exception_handler` logging `exc.errors()` verbatim, which
# includes pydantic v2's raw submitted `input` value for every failing
# field by default. A malformed POST /v1/auth/login (e.g. an over-length
# password, failing AdminLoginRequest.password's own
# Field(max_length=256) bound) wrote the plaintext attempted password
# straight into error.log before the fix in app/shared/responses.py
# (`redact_validation_errors`) + app/main.py.
#
# This test deliberately imports the REAL `app.main.app` (not this file's
# own simplified `_build_test_app()` reimplementation used everywhere
# above) and hits it with no ASGI lifespan/startup (FastAPI's
# `startup_event` - HF pipeline preload, ARQ redis pool - never fires over
# a plain `ASGITransport` without explicit lifespan handling, matching how
# `tests/conftest.py`'s own fixtures already avoid needing it): POST
# /auth/login only touches Mongo via app.services.auth, and
# `app.state.limiter` is set at module scope in app/main.py, not inside
# the startup hook, so both are available regardless.
#
# `app.main` itself cannot be imported in every checkout of this repo
# (see `tests/conftest.py`'s own module docstring - it needs `transformers`/
# `torch`, which this lightweight dev checkout doesn't have installed) -
# skipped, not faked, in that case, so this test only ever verifies the
# REAL production handler, never a reimplementation of it.
try:
    from app.main import app as _real_app

    _REAL_APP_IMPORT_ERROR = None
except Exception as e:  # pragma: no cover - see comment above
    _real_app = None
    _REAL_APP_IMPORT_ERROR = e


@pytest.mark.skipif(
    _real_app is None,
    reason=f"app.main not importable in this checkout: {_REAL_APP_IMPORT_ERROR!r}",
)
async def test_real_app_never_logs_plaintext_password_on_malformed_login(caplog):
    caplog.set_level(logging.WARNING)
    distinctive_password = "S3cr3t-OverLong-Plaintext-Marker-" + ("x" * 300)
    assert len(distinctive_password) > 256  # must actually violate Field(max_length=256)

    transport = ASGITransport(app=_real_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await ac.post(
            "/v1/auth/login",
            json={"email": "irrelevant-for-this-test@pdfconverterai.com", "password": distinctive_password},
        )

    assert resp.status_code == 422
    for record in caplog.records:
        assert distinctive_password not in record.getMessage()
        assert distinctive_password not in str(record.args or "")
