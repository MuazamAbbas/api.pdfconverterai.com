"""Regression coverage for the `_build_speed_trace_config` relocation
(Handbook Part C.3 module-boundary decision, follow-up to the
`check_url()`/`_MAX_REDIRECT_HOPS` relocation in the same
docs/roadmap/SPRINT_STATUS.md 2026-08-31 "feature-spec approved: SEO Audit"
entry): moved from `app/routers/web_tools.py` into
`app/shared/web/speed_trace.py`, a pure move with no behavior change, so
that `app/services/seo/seo_audit.py` can reuse it without a service
importing from a router.

Mirrors `tests/test_redirect_fetch_relocation.py`'s pattern for the sibling
`check_url` relocation. Trace-hook *behavior* itself (DNS/connect/TTFB
timing capture) continues to be covered end-to-end via `speed_test`'s
existing tests in `tests/test_web_tools_uptime_dns_ssl.py` (unchanged by
this relocation).
"""
import aiohttp
import pytest

import app.routers.web_tools as web_tools_router
import app.shared.web.speed_trace as speed_trace

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_build_speed_trace_config_is_the_same_function_object_in_both_modules():
    """`web_tools.py` imports `_build_speed_trace_config` back from the
    shared module rather than redefining it."""
    assert web_tools_router._build_speed_trace_config is speed_trace._build_speed_trace_config


async def test_build_speed_trace_config_still_importable_and_callable_from_shared_module():
    """Minimal live exercise from its new home - confirms the relocated
    function still builds a working `aiohttp.TraceConfig` that writes into
    the caller-owned `timings` dict."""
    timings: dict = {}
    trace_config = speed_trace._build_speed_trace_config(timings)
    assert isinstance(trace_config, aiohttp.TraceConfig)

    class _Ctx:
        pass

    ctx = _Ctx()
    await trace_config.on_dns_resolvehost_start[0](None, ctx, None)
    await trace_config.on_dns_resolvehost_end[0](None, ctx, None)
    assert "dns_time_ms" in timings
