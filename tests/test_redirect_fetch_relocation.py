"""Regression coverage for the `check_url()`/`_MAX_REDIRECT_HOPS` relocation
(Handbook Part C.3 module-boundary decision, docs/roadmap/SPRINT_STATUS.md
2026-08-31 "feature-spec approved: SEO Audit" entry): both moved from
`app/routers/web_tools.py` into `app/shared/web/redirect_fetch.py`, a pure
move with no behavior change, so that `app/services/seo/seo_audit.py` can
reuse them too without a router-to-router import.

This file only asserts the relocation itself is intact - `check_url()`'s own
behavior (redirect-following, SSRF re-validation per hop, retry-on-429,
etc.) is already covered exhaustively by
`tests/test_web_tools_uptime_dns_ssl.py`'s
`test_check_url_*`/`test_website_down_detector_*` tests, which continue to
run unchanged against `web_tools_router.check_url` post-relocation (see that
file - no edits were needed there, which is itself part of what "pure
relocation" means here).
"""
import aiohttp
import pytest

import app.routers.web_tools as web_tools_router
import app.shared.web.redirect_fetch as redirect_fetch

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_check_url_is_the_same_function_object_in_both_modules():
    """`web_tools.py` imports `check_url` back from the shared module rather
    than redefining it - so `web_tools_router.check_url` and
    `redirect_fetch.check_url` must be identical, not just equivalent, and
    monkeypatching one from a test (the existing convention throughout
    `test_web_tools_*.py`) transparently affects the other."""
    assert web_tools_router.check_url is redirect_fetch.check_url


async def test_max_redirect_hops_constant_is_shared_not_duplicated():
    assert web_tools_router._MAX_REDIRECT_HOPS is redirect_fetch._MAX_REDIRECT_HOPS
    assert web_tools_router._MAX_REDIRECT_HOPS == 5


async def test_redirect_statuses_constant_is_shared_not_duplicated():
    assert web_tools_router._REDIRECT_STATUSES is redirect_fetch._REDIRECT_STATUSES


async def test_redact_url_credentials_is_shared_not_duplicated():
    assert web_tools_router._redact_url_credentials is redirect_fetch._redact_url_credentials


async def test_redact_url_credentials_still_strips_userinfo():
    """Sanity check the relocated function still behaves correctly, not
    just that it's importable."""
    result = redirect_fetch._redact_url_credentials("https://user:pass@example.com/path")
    assert result == "https://example.com/path"
    assert "user" not in result
    assert "pass" not in result


async def test_check_url_still_importable_and_callable_from_shared_module():
    """A minimal live exercise of `check_url()` from its new home, using a
    fake session (no real network I/O) - mirrors
    `test_web_tools_uptime_dns_ssl.py`'s `_ScriptedRedirectSession` idiom at
    a small scale, just to confirm the relocated function still runs
    end-to-end from `app.shared.web.redirect_fetch` directly (not only when
    reached via `web_tools_router`)."""

    class _FakeResponse:
        def __init__(self, status: int):
            self.status = status
            self.headers: dict = {}
            self.request_info = aiohttp.RequestInfo(
                url="http://example.invalid", method="GET", headers={}, real_url="http://example.invalid",
            )
            self.history = ()

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

    class _FakeSession:
        def get(self, url, allow_redirects=False, timeout=5):
            return _FakeResponse(200)

    is_up, status = await redirect_fetch.check_url(_FakeSession(), "http://1.1.1.1/")
    assert is_up is True
    assert status == 200
