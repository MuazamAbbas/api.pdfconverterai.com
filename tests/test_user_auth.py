"""Unit + HTTP tests for the Public User Auth surface (`auth` module,
ADR-020, approved) - signup, login, session, password-reset.

Mirrors `tests/test_auth.py`'s structure/conventions closely (same
`_build_test_app`/`client`/`_reset_rate_limiter` shape), but every fixture/
assertion here targets the `users` collection and the new
`user_session`/`USER_JWT_SECRET`/`user_access` isolation boundary, never
`admin_users`/`ADMIN_JWT_SECRET`/`admin_access`. A handful of tests below
explicitly assert CROSS-isolation (an admin token can't satisfy
`require_user` and vice versa) - the concrete regression guard for ADR-020's
five-boundary table, on top of `test_auth.py`'s own admin-only coverage
staying green.

Uses the real local Mongo (`users` collection) and local Redis (ARQ), per
this suite's existing `tests/conftest.py` convention - never mocked, except
for the outbound Resend HTTP call itself (`app.services.notification.
email_service.send_email` is monkeypatched in the one test that exercises
the ARQ-enqueue path end-to-end, so this suite never makes a real network
call to a third-party API).
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

from app.core.admin_auth import ADMIN_COOKIE_NAME
from app.core.config import settings
from app.core.database import db
from app.core.rate_limiter import limiter
from app.core.user_auth import USER_COOKIE_NAME, require_user
from app.routers import auth as auth_router
from app.services.auth import user_service
from app.services.auth.password_service import hash_password, verify_password
from app.services.auth.token_service import create_admin_access_token
from app.services.auth.user_login_service import UserLoginResult, attempt_user_login
from app.services.auth.user_token_service import (
    create_user_access_token,
    decode_user_access_token,
)

# Same event-loop pinning as tests/test_auth.py - see that file's comment.
pytestmark = pytest.mark.asyncio(loop_scope="session")

_TEST_EMAIL = "seed-test-user@pdfconverterai.com"
_TEST_PASSWORD = "a-strong-test-password-123"


def _build_test_app() -> FastAPI:
    """Same shape as test_auth.py's `_build_test_app`, plus `app.state.
    arq_redis` (needed by POST /auth/users/password-reset/request, which
    enqueues a job - the admin routes never needed this)."""
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


class _FakeArqRedis:
    """Records `enqueue_job` calls instead of talking to a real Redis
    instance. The real `app.state.arq_redis.enqueue_job(...)` call shape
    used by `POST /auth/users/password-reset/request` is already the exact
    same call `app/routers/pdf.py` makes and that gets exercised against a
    real Redis/ARQ pool elsewhere in this suite (`tests/conftest.py`'s
    `test_app` fixture, used by the files/jobs flow tests) - this file
    isn't re-proving ARQ's own enqueue mechanism, only that this router
    calls it (or doesn't) under the right conditions, so a lightweight fake
    is enough here and keeps this whole test file runnable without a local
    Redis daemon."""

    def __init__(self):
        self.enqueued: list[tuple] = []

    async def enqueue_job(self, *args, **kwargs):
        self.enqueued.append((args, kwargs))


@pytest_asyncio.fixture
async def client():
    app = _build_test_app()
    app.state.arq_redis = _FakeArqRedis()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Exposed so tests can assert on what got (or didn't get) enqueued -
        # e.g. the ADR-020 Tier 1 -> Tier 2 handoff itself (job name/args)
        # for POST /auth/users/password-reset/request - without every other
        # test in this file needing to know this attribute exists.
        ac.fake_arq_redis = app.state.arq_redis
        yield ac


@pytest_asyncio.fixture
async def seeded_user():
    """Inserts one `users` document directly (bypassing POST /auth/signup),
    cleans it up afterward. Never touches any other document."""
    await db.users.delete_one({"email": _TEST_EMAIL})
    user = await user_service.create_user(email=_TEST_EMAIL, password_hash=hash_password(_TEST_PASSWORD))
    yield user
    await db.users.delete_one({"_id": user.id})


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_signup_emails():
    """Belt-and-suspenders cleanup for the handful of tests that actually
    call POST /auth/signup (rather than using `seeded_user`) - deletes any
    `users` document these tests created, by exact email, after the test
    runs. Never touches `seeded_user`'s own document (different email) or
    anything this test file didn't create."""
    created_emails: list[str] = []
    yield created_emails
    for email in created_emails:
        await db.users.delete_one({"email": email.strip().lower()})


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Same reasoning as test_auth.py's own fixture of the same name - the
    shared `limiter` singleton's in-memory counters persist across tests in
    this process otherwise."""
    limiter.reset()
    yield
    limiter.reset()


# --- user_service --------------------------------------------------------


async def test_create_user_rejects_duplicate_email(seeded_user):
    with pytest.raises(ValueError):
        await user_service.create_user(email=_TEST_EMAIL, password_hash=hash_password("some-other-password"))
    user_doc = await db.users.find_one({"email": _TEST_EMAIL})
    assert user_doc["_id"] == seeded_user.id


async def test_create_user_normalizes_email_case_and_whitespace():
    messy_email = "  Seed-Test-User@PDFConverterAI.com  "
    await db.users.delete_one({"email": _TEST_EMAIL})
    user = await user_service.create_user(email=messy_email, password_hash=hash_password(_TEST_PASSWORD))
    try:
        assert user.email == _TEST_EMAIL
        assert await user_service.get_user_by_email("SEED-test-USER@pdfconverterai.com  ") is not None
    finally:
        await db.users.delete_one({"_id": user.id})


# --- user_token_service ---------------------------------------------------


def test_create_and_decode_user_access_token():
    token = create_user_access_token(_TEST_EMAIL)
    payload = decode_user_access_token(token)
    assert payload is not None
    assert payload.sub == _TEST_EMAIL
    assert payload.type == "user_access"


def test_decode_user_access_token_rejects_bad_signature():
    token = create_user_access_token(_TEST_EMAIL)
    mid = len(token) // 2
    flipped_char = "A" if token[mid] != "A" else "B"
    tampered = token[:mid] + flipped_char + token[mid + 1 :]
    assert decode_user_access_token(tampered) is None


def test_decode_user_access_token_rejects_expired():
    from jose import jwt

    expired_payload = {
        "sub": _TEST_EMAIL,
        "iat": datetime.utcnow() - timedelta(hours=200),
        "exp": datetime.utcnow() - timedelta(hours=1),
        "type": "user_access",
    }
    expired_token = jwt.encode(expired_payload, settings.user_jwt_secret, algorithm=settings.user_jwt_algorithm)
    assert decode_user_access_token(expired_token) is None


def test_decode_user_access_token_rejects_wrong_type():
    from jose import jwt

    payload = {
        "sub": _TEST_EMAIL,
        "iat": datetime.utcnow(),
        "exp": datetime.utcnow() + timedelta(hours=1),
        "type": "not-user-access",
    }
    token = jwt.encode(payload, settings.user_jwt_secret, algorithm=settings.user_jwt_algorithm)
    assert decode_user_access_token(token) is None


# --- ADR-020 isolation: an admin token/secret can never satisfy require_user
# and vice versa (the actual regression guard for the five-boundary table) --


def test_admin_token_cannot_decode_as_user_token():
    admin_token = create_admin_access_token(_TEST_EMAIL)
    assert decode_user_access_token(admin_token) is None


def test_user_token_cannot_decode_as_admin_token():
    from app.services.auth.token_service import decode_admin_access_token

    user_token = create_user_access_token(_TEST_EMAIL)
    assert decode_admin_access_token(user_token) is None


class _FakeRequest:
    def __init__(self, cookies: dict):
        self.cookies = cookies


async def test_require_user_rejects_admin_session_cookie():
    """An admin_session cookie holding a perfectly valid ADMIN token must
    not satisfy require_user even if presented under the wrong cookie
    name - belt-and-suspenders on top of the secret/type check above."""
    admin_token = create_admin_access_token(_TEST_EMAIL)
    with pytest.raises(Exception) as exc_info:
        await require_user(_FakeRequest(cookies={USER_COOKIE_NAME: admin_token}))
    assert exc_info.value.status_code == 401


async def test_require_user_missing_cookie_raises_401():
    with pytest.raises(Exception) as exc_info:
        await require_user(_FakeRequest(cookies={}))
    assert exc_info.value.status_code == 401


async def test_require_user_valid_cookie_succeeds():
    token = create_user_access_token(_TEST_EMAIL)
    result = await require_user(_FakeRequest(cookies={USER_COOKIE_NAME: token}))
    assert result == {"email": _TEST_EMAIL}


# --- user_login_service: plaintext password must never be logged ---------


async def test_user_login_never_logs_plaintext_password(seeded_user, caplog):
    distinctive_password = "S3cr3t-User-Plaintext-Marker-Should-Never-Appear"
    caplog.set_level(logging.DEBUG)

    unknown_result, _ = await attempt_user_login("no-such-user-for-log-test@pdfconverterai.com", distinctive_password)
    wrong_result, _ = await attempt_user_login(_TEST_EMAIL, distinctive_password)

    assert unknown_result == UserLoginResult.DENIED
    assert wrong_result == UserLoginResult.DENIED
    assert len(caplog.records) > 0
    for record in caplog.records:
        assert distinctive_password not in record.getMessage()
        assert distinctive_password not in str(record.args or "")


# --- POST /v1/auth/signup -------------------------------------------------


async def test_signup_creates_account_and_sets_cookie(client, _cleanup_signup_emails):
    email = "new-signup-test-user@pdfconverterai.com"
    _cleanup_signup_emails.append(email)

    resp = await client.post(
        "/v1/auth/signup", json={"email": email, "password": "a-decent-password-1"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "token" not in body
    assert body["data"]["email"] == email

    set_cookie = resp.headers.get("set-cookie", "")
    assert USER_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie or "samesite=strict" in set_cookie.lower()

    user_doc = await db.users.find_one({"email": email})
    assert user_doc is not None
    assert verify_password("a-decent-password-1", user_doc["password_hash"])


async def test_signup_rejects_duplicate_email(client, seeded_user):
    resp = await client.post(
        "/v1/auth/signup", json={"email": _TEST_EMAIL, "password": "another-password-1"}
    )
    assert resp.status_code == 409
    assert resp.json()["success"] is False


async def test_signup_honeypot_silently_rejects_without_creating_account(client):
    email = "honeypot-bot-test-user@pdfconverterai.com"
    resp = await client.post(
        "/v1/auth/signup",
        json={"email": email, "password": "irrelevant-password-1", "website": "http://spam.example"},
    )
    # Looks exactly like a real success response...
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["email"] == email

    # ...but no account was actually created.
    assert await db.users.find_one({"email": email}) is None


# --- POST /v1/auth/users/login + /v1/auth/users/logout -------------------


async def test_user_login_success_sets_cookie_and_never_returns_token_in_body(client, seeded_user):
    resp = await client.post(
        "/v1/auth/users/login", json={"email": _TEST_EMAIL, "password": _TEST_PASSWORD}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert "token" not in body
    assert "data" in body and (body["data"] is None or "token" not in body["data"])

    set_cookie = resp.headers.get("set-cookie", "")
    assert USER_COOKIE_NAME in set_cookie
    assert "HttpOnly" in set_cookie
    assert "Secure" in set_cookie
    assert "SameSite=strict" in set_cookie or "samesite=strict" in set_cookie.lower()


async def test_user_login_wrong_password_and_unknown_email_return_identical_401(client, seeded_user):
    resp_wrong_password = await client.post(
        "/v1/auth/users/login", json={"email": _TEST_EMAIL, "password": "totally-wrong"}
    )
    resp_unknown_email = await client.post(
        "/v1/auth/users/login",
        json={"email": "no-such-user@pdfconverterai.com", "password": "totally-wrong"},
    )

    assert resp_wrong_password.status_code == 401
    assert resp_unknown_email.status_code == 401
    assert resp_wrong_password.json() == resp_unknown_email.json()
    assert "set-cookie" not in resp_wrong_password.headers
    assert "set-cookie" not in resp_unknown_email.headers


async def test_user_login_lockout_after_max_attempts(client, seeded_user):
    for _ in range(settings.user_login_max_attempts):
        resp = await client.post(
            "/v1/auth/users/login", json={"email": _TEST_EMAIL, "password": "totally-wrong"}
        )
        assert resp.status_code == 401

    locked_resp = await client.post(
        "/v1/auth/users/login", json={"email": _TEST_EMAIL, "password": _TEST_PASSWORD}
    )
    assert locked_resp.status_code == 401
    assert locked_resp.json()["message"] == "Invalid email or password"

    user_doc = await db.users.find_one({"email": _TEST_EMAIL})
    assert user_doc["locked_until"] is not None


async def test_user_logout_clears_cookie(client):
    resp = await client.post("/v1/auth/users/logout")
    assert resp.status_code == 200
    set_cookie = resp.headers.get("set-cookie", "")
    assert USER_COOKIE_NAME in set_cookie
    assert "Max-Age=0" in set_cookie


# --- GET /v1/auth/users/me -------------------------------------------------


async def test_get_current_user_valid_cookie_returns_email(client, seeded_user):
    # Set the cookie directly (rather than chaining off POST .../login's
    # Set-Cookie header) - that header carries `Secure`, which httpx's
    # cookie jar won't persist/replay over this test client's plain-http
    # `base_url` ("http://test"), same non-issue in production where the
    # site is always served over real HTTPS. `create_user_access_token` is
    # the exact same token-minting path the login route itself calls.
    token = create_user_access_token(_TEST_EMAIL)
    client.cookies.set(USER_COOKIE_NAME, token)

    resp = await client.get("/v1/auth/users/me")
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["data"]["email"] == _TEST_EMAIL


async def test_get_current_user_missing_cookie_returns_401_auth_required(client):
    resp = await client.get("/v1/auth/users/me")
    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "USER_AUTH_REQUIRED"


async def test_get_current_user_invalid_cookie_returns_401_auth_invalid(client):
    client.cookies.set(USER_COOKIE_NAME, "not-a-real-token-at-all")
    resp = await client.get("/v1/auth/users/me")
    assert resp.status_code == 401
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "USER_AUTH_INVALID"


# --- admin/user routes never collide, and admin cookie clearing doesn't
# touch the user cookie or vice versa -------------------------------------


async def test_admin_and_user_logout_touch_only_their_own_cookie(client):
    admin_resp = await client.post("/v1/auth/logout")
    assert ADMIN_COOKIE_NAME in admin_resp.headers.get("set-cookie", "")
    assert USER_COOKIE_NAME not in admin_resp.headers.get("set-cookie", "")

    user_resp = await client.post("/v1/auth/users/logout")
    assert USER_COOKIE_NAME in user_resp.headers.get("set-cookie", "")
    assert ADMIN_COOKIE_NAME not in user_resp.headers.get("set-cookie", "")


# --- POST /v1/auth/users/password-reset/request + /confirm ---------------


async def test_password_reset_request_always_returns_200_for_unknown_email(client):
    resp = await client.post(
        "/v1/auth/users/password-reset/request",
        json={"email": "no-such-account-anywhere@pdfconverterai.com"},
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True


async def test_password_reset_request_enqueues_send_password_reset_email_job(
    client, seeded_user, monkeypatch
):
    """The central ADR-020 Tier 1 -> Tier 2 handoff guarantee: a slow/
    unavailable Resend call can never block this synchronous request
    because the actual send is dispatched as an ARQ job, not made inline.
    Asserts the job name and arguments the router hands to
    `app.state.arq_redis.enqueue_job(...)`, not just that *some* 200 comes
    back (which the other tests in this section already cover but which
    doesn't, by itself, prove the handoff actually happened)."""
    real_issue = user_service.issue_password_reset_token
    captured = {}

    async def _capturing_issue(email):
        token = await real_issue(email)
        captured["token"] = token
        return token

    monkeypatch.setattr(user_service, "issue_password_reset_token", _capturing_issue)

    resp = await client.post(
        "/v1/auth/users/password-reset/request", json={"email": _TEST_EMAIL}
    )

    assert resp.status_code == 200

    enqueued = client.fake_arq_redis.enqueued
    assert len(enqueued) == 1
    args, kwargs = enqueued[0]
    assert args[:2] == ("send_password_reset_email", _TEST_EMAIL)
    assert kwargs == {}

    # The raw token is now wrapped in `pydantic.SecretStr`, not handed to
    # `enqueue_job` as a plain `str` (secret-leak fix - see
    # tests/test_worker_password_reset_secret_leak.py for the regression
    # test against arq's own job-argument logging, the actual leak
    # surface). `SecretStr` still round-trips the real value via
    # `.get_secret_value()`, but its `repr()` is redacted.
    from pydantic import SecretStr

    token_arg = args[2]
    assert isinstance(token_arg, SecretStr)
    assert token_arg.get_secret_value() == captured["token"]
    assert captured["token"] not in repr(token_arg)


async def test_password_reset_request_still_returns_200_when_enqueue_job_raises(
    client, seeded_user, monkeypatch
):
    """Simulates a Redis-down scenario: `enqueue_job` itself raises. The
    router's `except Exception` around the enqueue call must swallow this
    and still return the same 200 - the entire point of that try/except
    (ADR-020 Trade-offs: a queue outage delays, never breaks, this
    response) - currently unexercised without this test."""

    async def _boom(*args, **kwargs):
        raise ConnectionError("simulated redis outage")

    monkeypatch.setattr(client.fake_arq_redis, "enqueue_job", _boom)

    resp = await client.post(
        "/v1/auth/users/password-reset/request", json={"email": _TEST_EMAIL}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert (
        body["message"]
        == "If that email address has an account, a password reset link has been sent"
    )

    # The reset token was still minted/stored (Tier 1 work isn't rolled
    # back by a Tier 2 dispatch failure) - only the email dispatch job
    # itself failed to enqueue.
    user_doc = await db.users.find_one({"email": _TEST_EMAIL})
    assert user_doc["password_reset_token_hash"] is not None


async def test_password_reset_request_and_confirm_round_trip(client, seeded_user, monkeypatch):
    """End-to-end against the request/confirm HTTP endpoints themselves.
    `POST .../request` enqueues the actual Resend send as an ARQ job for
    `arq-worker.service` to consume (not running under pytest, so this test
    never triggers a real Resend call) - it only exercises the Tier 1 side:
    token mint/store + enqueue succeeding, then confirm accepting that exact
    token, changing the password, and the token being single-use.
    """
    # Capture the raw token directly from user_service (never exposed via
    # the HTTP response - that would itself be a leak) rather than
    # depending on the real ARQ worker process actually running.
    real_issue = user_service.issue_password_reset_token
    captured = {}

    async def _capturing_issue(email):
        token = await real_issue(email)
        captured["token"] = token
        return token

    monkeypatch.setattr(user_service, "issue_password_reset_token", _capturing_issue)

    resp = await client.post(
        "/v1/auth/users/password-reset/request", json={"email": _TEST_EMAIL}
    )
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    raw_token = captured["token"]
    assert raw_token is not None

    # The response body must never contain the raw token.
    assert raw_token not in resp.text

    new_password = "brand-new-password-456"
    confirm_resp = await client.post(
        "/v1/auth/users/password-reset/confirm",
        json={"token": raw_token, "new_password": new_password},
    )
    assert confirm_resp.status_code == 200
    assert confirm_resp.json()["success"] is True

    # New password now works; old one no longer does.
    login_ok = await client.post(
        "/v1/auth/users/login", json={"email": _TEST_EMAIL, "password": new_password}
    )
    assert login_ok.status_code == 200
    login_old = await client.post(
        "/v1/auth/users/login", json={"email": _TEST_EMAIL, "password": _TEST_PASSWORD}
    )
    assert login_old.status_code == 401

    # Single-use: the exact same token can never be replayed.
    replay_resp = await client.post(
        "/v1/auth/users/password-reset/confirm",
        json={"token": raw_token, "new_password": "yet-another-password-789"},
    )
    assert replay_resp.status_code == 400


async def test_password_reset_confirm_rejects_garbage_token(client):
    resp = await client.post(
        "/v1/auth/users/password-reset/confirm",
        json={"token": "not-a-real-token-at-all-xxxxxxxx", "new_password": "whatever-password-1"},
    )
    assert resp.status_code == 400
    assert resp.json()["success"] is False


async def test_password_reset_confirm_rejects_expired_token(client, seeded_user):
    raw_token = await user_service.issue_password_reset_token(_TEST_EMAIL)
    # Force it into the past directly (bypassing the normal expiry window)
    # rather than waiting real time.
    await db.users.update_one(
        {"_id": seeded_user.id},
        {"$set": {"password_reset_expires_at": datetime.utcnow() - timedelta(minutes=1)}},
    )
    resp = await client.post(
        "/v1/auth/users/password-reset/confirm",
        json={"token": raw_token, "new_password": "whatever-password-2"},
    )
    assert resp.status_code == 400


# --- notification.email_service -------------------------------------------


async def test_send_email_raises_on_unknown_template():
    from app.services.notification.email_service import EmailSendError, send_email

    with pytest.raises(EmailSendError):
        await send_email("someone@example.com", "not-a-real-template", {})
