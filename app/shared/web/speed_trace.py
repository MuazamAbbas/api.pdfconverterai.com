"""Shared `aiohttp` trace-config builder for phase-level HTTP timing
(Handbook Part C.3 / D.3 - `app/shared/` holds code reused across router
modules without routers importing each other directly, or a service
importing from a router, which the one-module-one-responsibility rule
forbids).

Relocated here from `app/routers/web_tools.py` (pure move, no behavior
change - see docs/roadmap/SPRINT_STATUS.md 2026-08-31 "feature-spec
approved: SEO Audit" entry) because `app/services/seo/seo_audit.py`'s
`_fetch_main_page` needs the same per-hop DNS/connect/TTFB timing hooks
`web_tools.py`'s `speed_test` already uses. `seo_audit.py` previously
imported `_build_speed_trace_config` directly from `app.routers.web_tools`
- a service importing from a router - which this relocation closes, mirroring
the `check_url` move into `app/shared/web/redirect_fetch.py` in the same
change.

`web_tools.py` imports `_build_speed_trace_config` back from here so its
existing caller (`speed_test`) keeps working unchanged.
"""
import time

import aiohttp


def _build_speed_trace_config(timings: dict) -> aiohttp.TraceConfig:
    """Registers `aiohttp` trace hooks to capture per-phase timings into the
    caller-owned `timings` dict (each hook writes a key on completion; the
    dict is `.clear()`-ed by the caller at the start of every redirect hop,
    so only the *final* hop's phase timings survive to the response -
    `total_time_ms`, tracked separately by the caller around the whole
    redirect loop, still reflects the full multi-hop duration).

    DNS (`on_dns_resolvehost_*`) and connection-create
    (`on_connection_create_*`) hooks bracket resolution/socket-connect time
    directly - each hook pair receives the same per-request
    `trace_config_ctx` object, used here as scratch space for the "start"
    timestamp.

    TTFB is captured via `on_request_start`/`on_request_end` rather than
    `on_response_chunk_received`'s first call: reading `aiohttp`'s own
    `ClientSession._request()` (in `client.py`) shows `on_request_end` fires
    immediately after `resp.start(conn)` - the call that reads the status
    line and headers - and *before* the response body is read (body reading
    is the caller's job, done separately via `resp.read()` below). That
    makes `on_request_end` a direct, accurate "headers received" marker,
    not an approximation via first-body-chunk timing (which would also
    fold in any gap the server has between sending headers and starting
    the body).

    `on_request_start` fires before DNS resolution/connection-establishment
    for that request even begin, so `ttfb_ms` as captured here is
    INCLUSIVE of `dns_time_ms` + `connect_time_ms`, not an isolated
    "waiting after connect" phase - the same inclusive definition common
    PageSpeed-style tools use for "time to first byte" (full
    request-to-first-byte wall clock, not connection-established-to-first-
    byte). `dns_time_ms`/`connect_time_ms` overlapping with `ttfb_ms` in
    the response is therefore intentional, not double-counting to "fix".
    """
    trace_config = aiohttp.TraceConfig()

    async def on_dns_start(session, ctx, params):
        ctx.dns_start = time.monotonic()

    async def on_dns_end(session, ctx, params):
        if hasattr(ctx, "dns_start"):
            timings["dns_time_ms"] = round((time.monotonic() - ctx.dns_start) * 1000, 2)

    async def on_connect_start(session, ctx, params):
        ctx.connect_start = time.monotonic()

    async def on_connect_end(session, ctx, params):
        if hasattr(ctx, "connect_start"):
            timings["connect_time_ms"] = round((time.monotonic() - ctx.connect_start) * 1000, 2)

    async def on_req_start(session, ctx, params):
        ctx.req_start = time.monotonic()

    async def on_req_end(session, ctx, params):
        if hasattr(ctx, "req_start"):
            timings["ttfb_ms"] = round((time.monotonic() - ctx.req_start) * 1000, 2)

    trace_config.on_dns_resolvehost_start.append(on_dns_start)
    trace_config.on_dns_resolvehost_end.append(on_dns_end)
    trace_config.on_connection_create_start.append(on_connect_start)
    trace_config.on_connection_create_end.append(on_connect_end)
    trace_config.on_request_start.append(on_req_start)
    trace_config.on_request_end.append(on_req_end)
    return trace_config
