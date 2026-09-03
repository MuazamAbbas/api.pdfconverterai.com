"""Regression test for a real, live-verified secret leak: `arq`'s own
default job-argument logging (`arq.worker.Worker.run_job`, via
`arq.utils.args_to_string()` calling `repr()` over every positional job
argument) logged the raw, unhashed password-reset token in plaintext on
every real `POST /auth/users/password-reset/request` -> `send_password_
reset_email` dispatch. A real production `journalctl -u arq-worker.service`
line looked like:

    07:44:13:   0.36s -> ...:send_password_reset_email('user@example.com', 'kRHkA...')

That second quoted string is the raw reset token - a bearer credential
equivalent to an account-takeover secret - sitting in plaintext in the VPS
system journal on every real password reset. Our own application code
never explicitly logged `email`/`reset_token` (see
`app/worker.py::send_password_reset_email`'s docstring) - the leak came
entirely from `arq`'s own internals, not any `logger.*` call this codebase
makes. That's exactly why `tests/test_auth.py::test_login_never_logs_
plaintext_password` and `test_real_app_never_logs_plaintext_password_on_
malformed_login` (which only assert our own `logger.*` calls are clean)
could never have caught this class of bug.

Unlike those two, this test does NOT call `app.worker.send_password_reset_
email` directly, nor does it fake arq the way `tests/test_user_auth.py`'s
`_FakeArqRedis` deliberately does (see that class's own docstring - a
lightweight fake is enough for *that* file's purposes, proving the router
calls `enqueue_job` with the right args, not exercising arq's own logging
internals). Instead this drives the FULL real path that produced the actual
leaked line above: a real HTTP request to `POST /auth/users/password-reset/
request` (exercising `app/routers/auth.py`'s actual `enqueue_job(...)` call,
whatever it currently passes) against a real local Redis-backed arq queue,
drained by a real `arq.worker.Worker` (`run_check()` - arq's own documented
test entry point, "Useful when testing" - burst mode) running the real
`app.worker.send_password_reset_email` task function, with `arq.worker`'s
logger captured via `caplog`. That's the same `Worker.run_job` orchestration
(job-start `... -> name(args)`/completion `... <- name` logging) the real
`arq-worker.service` process runs.

Fix (`app/routers/auth.py` + `app/worker.py`): the raw token is wrapped in
`pydantic.SecretStr` before being handed to `enqueue_job(...)`, instead of
a plain `str`. `SecretStr.__repr__()` returns the redacted
`SecretStr('**********')`, so `args_to_string()`'s `repr()` over the job's
positional args now embeds that redacted form in arq's log line instead of
the raw token - without disabling or reconfiguring arq's own logging (kept
intact for every other job type's debuggability). `.get_secret_value()` is
only ever called at the point `send_password_reset_email` composes the
real reset link, never logged.

Regression proof (per the task brief): this test file was verified this
session to FAIL against the pre-fix code (plain `str`, restored via
`git stash` of the `app/routers/auth.py` + `app/worker.py` diff) and PASS
against the post-fix code - both runs' actual pytest output are reported
alongside this change, not merely asserted here.
"""
import logging
import uuid

import pytest
import pytest_asyncio
from arq.connections import RedisSettings, create_pool
from arq.utils import args_to_string
from arq.worker import Worker
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.core.config import settings
from app.core.database import db
from app.core.rate_limiter import limiter
from app.routers import auth as auth_router
from app.services.auth import user_service
from app.services.auth.password_service import hash_password
from app.worker import send_password_reset_email

# Same session-scoped-loop pinning as the rest of this suite (see
# tests/conftest.py's own comment) - app.core.database.db's Motor client
# binds to whichever loop first touches it.
pytestmark = pytest.mark.asyncio(loop_scope="session")

_TEST_PASSWORD = "a-strong-test-password-123"

# Deliberately short - see `test_password_reset_token_never_appears_in_real_
# arq_job_logs`'s sanity assertion below. `arq.utils.args_to_string()` (what
# actually builds the string embedded in `arq.worker`'s job-start log line)
# truncates its output at `arq.utils.DEFAULT_CURTAIL` (80 chars) via
# `arq.utils.truncate()` - a long enough email would truncate the raw token
# out of the log line for reasons having nothing to do with this fix,
# silently making this test a false negative (it would "pass" even against
# genuinely leaking pre-fix code, simply because the leak got cut off by an
# unrelated string-length limit rather than actually redacted). The 43-char
# `secrets.token_urlsafe(32)` reset token (`user_service.issue_password_
# reset_token`) plus this short email plus `repr()`/formatting overhead
# stays comfortably under that limit - the sanity assertion below verifies
# this explicitly for whatever the real generated token turns out to be,
# rather than trusting this arithmetic to stay valid forever.
_TEST_EMAIL = "leak@pdfconverterai.com"


def _build_test_app(queue_name: str) -> FastAPI:
    """Same shape as `tests/test_user_auth.py`'s `_build_test_app`, but
    `app.state.arq_redis` is a REAL arq/Redis pool (not that file's
    deliberately-lightweight `_FakeArqRedis`) scoped to a unique
    `default_queue_name` per test run - this test needs the real
    `enqueue_job` -> real queue -> real `Worker.run_job` round trip that a
    fake can't provide, since the leak lives in arq's own job-logging
    internals.
    """
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
async def seeded_user():
    """Inserts one `users` document directly, cleans it up afterward -
    identical convention to `tests/test_user_auth.py`'s fixture of the same
    name, kept local to this file so this regression test has no
    cross-file fixture dependency."""
    await db.users.delete_one({"email": _TEST_EMAIL})
    user = await user_service.create_user(email=_TEST_EMAIL, password_hash=hash_password(_TEST_PASSWORD))
    yield user
    await db.users.delete_one({"_id": user.id})


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Same reasoning as `tests/test_auth.py`/`tests/test_user_auth.py`'s
    own fixture of this name - `app.core.rate_limiter.limiter` is a
    process-wide singleton shared across every test module in the same
    pytest session."""
    limiter.reset()
    yield
    limiter.reset()


async def test_password_reset_token_never_appears_in_real_arq_job_logs(
    seeded_user, monkeypatch, caplog
):
    """End-to-end: real `POST /auth/users/password-reset/request` -> real
    arq enqueue -> real `arq.worker.Worker.run_job` drain -> real
    `app.worker.send_password_reset_email`. Asserts the raw reset token
    never appears in any `arq.worker` log record, while the job still
    completes and the real email (faked at the outbound-HTTP boundary
    only) still gets the correct, unmasked reset link.
    """
    sent_calls = []

    async def _fake_send_email(to, template, context):
        sent_calls.append({"to": to, "template": template, "context": context})

    # `send_password_reset_email` re-imports `send_email` from this exact
    # module on every call (see its own docstring/body in app/worker.py),
    # so patching the source module's attribute takes effect.
    monkeypatch.setattr(
        "app.services.notification.email_service.send_email", _fake_send_email
    )

    captured = {}
    real_issue = user_service.issue_password_reset_token

    async def _capturing_issue(email):
        token = await real_issue(email)
        captured["token"] = token
        return token

    monkeypatch.setattr(user_service, "issue_password_reset_token", _capturing_issue)

    queue_name = f"test-password-reset-secret-leak-{uuid.uuid4().hex}"
    redis_settings = RedisSettings.from_dsn(settings.redis_url)

    app = _build_test_app(queue_name)
    app.state.arq_redis = await create_pool(redis_settings, default_queue_name=queue_name)
    try:
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.post(
                "/v1/auth/users/password-reset/request", json={"email": _TEST_EMAIL}
            )
        assert resp.status_code == 200
        assert "token" in captured, "the endpoint must have actually issued a reset token"
        raw_token = captured["token"]

        # Sanity guard (see `_TEST_EMAIL`'s comment above): confirms - via
        # arq's own real `args_to_string()`, not a reimplementation - that
        # a plain, unwrapped `(email, raw_token)` positional-argument pair
        # would NOT get truncated before the token, for this exact
        # generated token. If this ever fails, the test below would be a
        # false negative rather than a real regression guard, and must be
        # fixed (e.g. a shorter `_TEST_EMAIL`) rather than ignored.
        assert raw_token in args_to_string((_TEST_EMAIL, raw_token), {}), (
            "test precondition violated: arq's own args_to_string() would truncate "
            "the raw token out of its log line for unrelated string-length reasons, "
            "making this test unable to detect a real regression - shorten _TEST_EMAIL"
        )

        worker = Worker(
            functions=[send_password_reset_email],
            redis_settings=redis_settings,
            queue_name=queue_name,
            burst=True,
            poll_delay=0.05,
            handle_signals=False,
            max_tries=1,
        )
        try:
            with caplog.at_level(logging.INFO, logger="arq.worker"):
                await worker.run_check()
        finally:
            # Not `await worker.close()`: arq's own `Worker.close()`
            # unconditionally calls `self.handle_sig(signal.SIGUSR1)` when
            # `handle_signals=False` (its documented "useful when testing"
            # mode - see `arq/worker.py`), and `signal.SIGUSR1` doesn't
            # exist on Windows (this dev checkout's platform) - an arq/
            # platform quirk unrelated to this test's actual subject
            # (the real `arq-worker.service` process on the Linux VPS/CI
            # runner never hits this path at all, since it always runs
            # with real OS signal handling). Replicates just the pool
            # cleanup `close()` would otherwise also do.
            if worker._pool is not None:
                await worker.pool.delete(worker.health_check_key)
                await worker.pool.aclose()
    finally:
        await app.state.arq_redis.aclose()

    # Sanity: the job actually ran end to end (a silent no-op regression
    # would otherwise make every assertion below trivially/falsely pass).
    assert len(sent_calls) == 1
    assert sent_calls[0]["to"] == _TEST_EMAIL
    assert sent_calls[0]["template"] == "password_reset"
    # SecretStr must not break the feature while fixing the leak: the real
    # reset link handed to the (faked) email send still contains the real,
    # unwrapped token.
    assert sent_calls[0]["context"]["reset_link"].endswith(f"token={raw_token}")

    assert len(caplog.records) > 0, "sanity: arq.worker must have logged something"
    # Sanity: this actually captured the specific job-start/completion log
    # line this regression test targets (arq.worker.Worker.run_job's
    # '... -> name(args)...' format), not just unrelated arq.worker chatter.
    assert any("send_password_reset_email(" in r.getMessage() for r in caplog.records), (
        "did not capture the job-start log line this regression test targets"
    )

    for record in caplog.records:
        message = record.getMessage()
        assert raw_token not in message, (
            f"raw password-reset token leaked into an arq.worker log message: {message!r}"
        )
        assert raw_token not in str(record.args or ""), (
            f"raw password-reset token leaked into an arq.worker log record.args: {record.args!r}"
        )
