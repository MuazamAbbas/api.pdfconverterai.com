"""SSRF-hardening coverage for `app/services/web_tools/summarize.py::
fetch_webpage_text()` (Handbook Part C.10, closing `api.pdfconverterai.com#98`
- the last PR #97 gap, deliberately deferred there since `fetch_webpage_text`
needed the response *body*, not just a reachability verdict).

Mirrors the existing mocking conventions already established for this
severity class rather than inventing new ones:
- `tests/test_seo_audit_service.py`'s `_FakeMainPageResponse`/
  `_FakeMainPageSession` idiom (patch `aiohttp.ClientSession` at the
  `summarize` module's own call site, fake `.content.read()`/redirect
  `Location` header) for the redirect-hop and happy-path cases.
- `tests/test_network_security_pinning.py`'s rebinding-`getaddrinfo` mock
  for the DNS-rebinding and hostname-resolves-to-private cases.
- Literal-IP cases are exercised for real (no mocking), matching
  `test_network_security_pinning.py`'s own convention for IP-literal input
  - `getaddrinfo()` resolves a literal IP to itself with no network I/O.

No real network call is ever made anywhere in this file.
"""
import asyncio
import socket
import time

import aiohttp
import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

import app.services.web_tools.summarize as summarize
import app.shared.network_security as network_security
from app.shared.network_security import UnsafeHostError
from app.shared.web.pinned_resolver import SafeResolver

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SAFE_IP = "93.184.216.34"
_UNSAFE_IP = "127.0.0.1"


# ===========================================================================
# Shared fakes (mirrors tests/test_seo_audit_service.py's
# _FakeMainPageResponse/_FakeMainPageSession exactly, adapted for
# summarize.py's `response.raise_for_status()` call).
# ===========================================================================

class _FakeSummarizeResponse:
    def __init__(
        self,
        status: int,
        location: str | None = None,
        body: bytes = b"",
        max_read_chunk: int | None = None,
    ):
        self.status = status
        self.headers = {"Location": location} if location else {}
        self._body = body
        self._pos = 0
        # None = serve up to the requested `n` per call (still EOF-correct,
        # via the real cursor below) - a small int instead simulates a real
        # chunked-transfer-encoded/TCP-segmented remote that hands back far
        # fewer bytes than requested per read, exercising
        # `_read_body_capped()`'s multi-read accumulation loop (closing the
        # gap where `StreamReader.read(n)` returning less than `n` per call
        # was previously untested - a bare single-call fake would have hidden
        # the bug the real fix addresses).
        self._max_read_chunk = max_read_chunk
        self.read_call_count = 0
        self.content = self
        self.request_info = aiohttp.RequestInfo(
            url="http://example.invalid", method="GET", headers={}, real_url="http://example.invalid",
        )
        self.history = ()

    async def read(self, n: int) -> bytes:
        # Real cursor (unlike the original bare `self._body[:n]`), so a
        # second call correctly continues where the first left off and
        # returns `b""` at EOF - matching real `StreamReader.read()`
        # semantics instead of re-serving the same prefix forever.
        self.read_call_count += 1
        limit = n if self._max_read_chunk is None else min(n, self._max_read_chunk)
        chunk = self._body[self._pos : self._pos + limit]
        self._pos += len(chunk)
        return chunk

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise aiohttp.ClientResponseError(
                request_info=self.request_info, history=self.history, status=self.status,
            )

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeSummarizeSession:
    def __init__(self, responses_by_url: dict):
        self._responses = responses_by_url
        self.requested_urls: list[str] = []

    def get(self, url, allow_redirects=False):
        self.requested_urls.append(url)
        return self._responses[url]

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


def _patch_session(monkeypatch, session):
    def _factory(*args, **kwargs):
        return session

    monkeypatch.setattr(summarize.aiohttp, "ClientSession", _factory)


def _make_rebinding_getaddrinfo(answers: list[str]):
    """Same technique as `tests/test_network_security_pinning.py`: answers
    `answers[0]` on the first call, sticking on `answers[-1]` for every call
    after - simulating a malicious short-TTL DNS server rebinding between a
    "check" resolution and a later "connect" resolution for the same
    hostname. `calls["n"]` counts how many real resolutions happened."""
    calls = {"n": 0}

    def _fake(host, port=None, *args, **kwargs):
        index = min(calls["n"], len(answers) - 1)
        calls["n"] += 1
        ip = answers[index]
        family = socket.AF_INET6 if ":" in ip else socket.AF_INET
        sockaddr = (ip, 0, 0, 0) if family == socket.AF_INET6 else (ip, 0)
        return [(family, socket.SOCK_STREAM, 6, "", sockaddr)]

    return _fake, calls


# ===========================================================================
# Blocked: literal private/loopback/link-local/metadata IPs - exercised for
# real, no mocking (getaddrinfo resolves a literal IP to itself).
# ===========================================================================

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/",
    "http://169.254.169.254/",
    "http://10.0.0.5/",
    "http://172.16.0.5/",
    "http://192.168.1.5/",
    "http://[::1]/",
    "http://[fc00::1]/",
    "http://[fe80::1]/",
])
async def test_literal_private_loopback_link_local_metadata_ip_blocked(url):
    with pytest.raises(summarize.WebpageFetchBlockedError):
        await summarize.fetch_webpage_text(url)


async def test_ipv4_mapped_ipv6_literal_blocked():
    """Python's `ipaddress.IPv6Address.is_loopback` already correctly flags
    an IPv4-mapped IPv6 address (`::ffff:127.0.0.1`) - confirmed directly
    against `_is_unsafe_ip()` during the approved spec's pre-work, no
    changes needed there. This just confirms `fetch_webpage_text` inherits
    that for free via `assert_host_is_safe()`."""
    with pytest.raises(summarize.WebpageFetchBlockedError):
        await summarize.fetch_webpage_text("http://[::ffff:127.0.0.1]/")


# ===========================================================================
# Blocked: a hostname that resolves (via DNS) to a private address.
# ===========================================================================

async def test_hostname_resolving_to_private_ip_blocked(monkeypatch):
    fake_getaddrinfo, _calls = _make_rebinding_getaddrinfo([_UNSAFE_IP])
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    with pytest.raises(summarize.WebpageFetchBlockedError):
        await summarize.fetch_webpage_text("http://evil.example.com/")


# ===========================================================================
# Blocked: a redirect hop targeting a private address - re-validated and
# refused per hop, mirroring check_url()'s exact contract.
# ===========================================================================

async def test_redirect_to_private_address_blocked(monkeypatch):
    # Uses a real public IP literal (`1.1.1.1`), not a hostname, as the
    # starting URL - matching `tests/test_web_tools_uptime_dns_ssl.py`'s own
    # `check_url()` redirect tests - so the pre-request
    # `assert_host_is_safe()` check resolves for real (an IP-literal
    # resolves to itself, no network I/O) without needing a DNS mock.
    session = _FakeSummarizeSession({
        "http://1.1.1.1/": _FakeSummarizeResponse(302, location="http://169.254.169.254/latest/meta-data/"),
    })
    _patch_session(monkeypatch, session)

    with pytest.raises(summarize.WebpageFetchBlockedError):
        await summarize.fetch_webpage_text("http://1.1.1.1/")

    # The unsafe redirect target was never actually requested.
    assert session.requested_urls == ["http://1.1.1.1/"]


# ===========================================================================
# DNS-rebinding simulation - same technique as
# tests/test_network_security_pinning.py, applied to the exact resolver
# `summarize._safe_connector()` wires every real connection through: proves
# the connect-time pin comes from one atomic resolve-and-validate call, not
# a second, independently re-resolvable one a short-TTL rebind could win.
# ===========================================================================

async def test_safe_connector_resolver_pins_first_resolution_not_the_rebind_target(monkeypatch):
    fake_getaddrinfo, calls = _make_rebinding_getaddrinfo([_SAFE_IP, _UNSAFE_IP])
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    connector = summarize._safe_connector()
    assert isinstance(connector._resolver, SafeResolver), (
        "fetch_webpage_text's session must connect through the same "
        "DNS-rebinding-proof SafeResolver every other PR #97 caller uses, "
        "not a default/unpinned resolver"
    )

    results = await connector._resolver.resolve("rebinding.example.com", port=443)

    assert calls["n"] == 1, "the resolver must resolve exactly once"
    assert len(results) == 1
    assert results[0]["host"] == _SAFE_IP

    # Prove the rebind would actually have happened on any later, separate
    # resolution - the exact race this closes.
    second_answer = fake_getaddrinfo("rebinding.example.com", 443)
    assert second_answer[0][4][0] == _UNSAFE_IP
    assert calls["n"] == 2

    await connector.close()


async def test_safe_connector_resolver_rejects_when_the_single_resolution_is_unsafe(monkeypatch):
    fake_getaddrinfo, calls = _make_rebinding_getaddrinfo([_UNSAFE_IP])
    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)

    connector = summarize._safe_connector()
    with pytest.raises(UnsafeHostError):
        await connector._resolver.resolve("evil.example.com")

    assert calls["n"] == 1
    await connector.close()


# ===========================================================================
# Allowed: a normal public URL still summarizes successfully.
# ===========================================================================

async def test_public_url_still_fetches_and_extracts_text(monkeypatch):
    # Real public IP literal as the URL host (see
    # test_redirect_to_private_address_blocked's comment) - avoids a real
    # DNS lookup for a hostname like "example.com" while still exercising
    # the real, unmocked `assert_host_is_safe()` pre-check.
    html = (
        b"<html><body>"
        b"<p>This is a perfectly normal public webpage paragraph, long "
        b"enough to clear the 50 character minimum easily.</p>"
        b"</body></html>"
    )
    session = _FakeSummarizeSession({
        "http://1.1.1.1/article": _FakeSummarizeResponse(200, body=html),
    })
    _patch_session(monkeypatch, session)

    text = await summarize.fetch_webpage_text("http://1.1.1.1/article")

    assert "perfectly normal public webpage paragraph" in text
    assert session.requested_urls == ["http://1.1.1.1/article"]


async def test_public_url_following_one_safe_redirect_still_succeeds(monkeypatch):
    html = (
        b"<html><body><p>Final destination paragraph text, long enough "
        b"to clear the fifty character minimum requirement here.</p></body></html>"
    )
    session = _FakeSummarizeSession({
        "http://1.1.1.1/old": _FakeSummarizeResponse(301, location="http://9.9.9.9/new"),
        "http://9.9.9.9/new": _FakeSummarizeResponse(200, body=html),
    })
    _patch_session(monkeypatch, session)

    text = await summarize.fetch_webpage_text("http://1.1.1.1/old")

    assert "Final destination paragraph text" in text
    assert session.requested_urls == ["http://1.1.1.1/old", "http://9.9.9.9/new"]


# ===========================================================================
# Response-size cap - an explicit bound added by this change (previously
# `await response.text()` read the body fully, unbounded).
# ===========================================================================

async def test_fetch_safe_caps_response_body_at_max_body_bytes():
    oversized_body = b"a" * (summarize._MAX_BODY_BYTES + 500)
    session = _FakeSummarizeSession({
        "http://1.1.1.1/huge": _FakeSummarizeResponse(200, body=oversized_body),
    })

    text = await summarize._fetch_safe(session, "http://1.1.1.1/huge")

    assert len(text) == summarize._MAX_BODY_BYTES


async def test_fetch_safe_assembles_full_body_from_many_small_chunks_under_cap():
    """`response.content.read(n)` on a real connection can return far fewer
    than `n` bytes per call (not "n bytes or EOF") - this forces exactly
    that with `max_read_chunk=7`, so the body can only be assembled
    correctly if `_read_body_capped()` actually loops rather than trusting
    a single `read()` call to return everything up to the cap."""
    body = (
        b"<html><body><p>Multi-chunk paragraph text, long enough to clear "
        b"the fifty character minimum, served seven bytes at a time.</p>"
        b"</body></html>"
    )
    response = _FakeSummarizeResponse(200, body=body, max_read_chunk=7)
    session = _FakeSummarizeSession({"http://1.1.1.1/chunked": response})

    text = await summarize._fetch_safe(session, "http://1.1.1.1/chunked")

    assert text == body.decode("utf-8")
    # Proves multiple reads actually happened - a broken single-read
    # implementation would have returned only the first 7 bytes.
    assert response.read_call_count > len(body) // 7


async def test_fetch_safe_stops_reading_once_over_cap_without_draining_whole_stream():
    """An over-cap body must be cut at `_MAX_BODY_BYTES` without the loop
    draining the rest of the (potentially huge/malicious) remote stream -
    proven here by a stream twice the cap size, served in small chunks, and
    asserting far fewer reads happened than a full drain would require."""
    chunk_size = 1_000
    oversized_body = b"a" * (summarize._MAX_BODY_BYTES * 2)
    response = _FakeSummarizeResponse(200, body=oversized_body, max_read_chunk=chunk_size)
    session = _FakeSummarizeSession({"http://1.1.1.1/huge-chunked": response})

    text = await summarize._fetch_safe(session, "http://1.1.1.1/huge-chunked")

    assert len(text) == summarize._MAX_BODY_BYTES
    full_drain_call_count = len(oversized_body) // chunk_size
    # Expected to stop at ceil(_MAX_BODY_BYTES / chunk_size) + 1 reads (one
    # past crossing the cap); allow a little slack but stay well short of
    # the full-drain count, which is double the cap's worth of chunks here.
    expected_stop_call_count = summarize._MAX_BODY_BYTES // chunk_size + 1
    assert response.read_call_count <= expected_stop_call_count + 5
    assert response.read_call_count < full_drain_call_count, (
        "must stop shortly after crossing the cap, not read the entire "
        "oversized stream"
    )


# ===========================================================================
# Shared ~30s deadline (founder decision (a)) covers the whole fetch -
# every redirect hop plus the body read - not a per-hop budget that resets.
# ===========================================================================

class _SlowFakeResponse:
    """The per-request object whose `__aenter__` hangs - entering it happens
    inside `_fetch_safe()`'s loop, i.e. inside the `asyncio.wait_for(...)`
    this test is actually proving cancels it."""

    async def __aenter__(self):
        await asyncio.sleep(5.0)
        raise AssertionError("the shared deadline should have cancelled this first")

    async def __aexit__(self, *exc_info):
        return False


class _SlowFakeSummarizeSession:
    """Simulates a request that never completes within the test's shortened
    deadline - used to prove `asyncio.wait_for` actually bounds the overall
    wall-clock time of `fetch_webpage_text()`, not just the connect phase.
    The session-level `async with` (outside `asyncio.wait_for` in
    `fetch_webpage_text()`) must return instantly - only `.get()`'s response
    hangs, so the hang is correctly inside the timed region."""

    def get(self, url, allow_redirects=False):
        return _SlowFakeResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


async def test_shared_deadline_bounds_the_whole_fetch(monkeypatch):
    monkeypatch.setattr(summarize, "_FETCH_TIMEOUT_SECONDS", 0.05)
    _patch_session(monkeypatch, _SlowFakeSummarizeSession())

    start = time.monotonic()
    with pytest.raises(aiohttp.ServerTimeoutError):
        await summarize.fetch_webpage_text("http://1.1.1.1/slow")
    elapsed = time.monotonic() - start

    assert elapsed < 2.0, "the shared deadline must cut the fetch off, not wait out the full delay"


# ===========================================================================
# Founder decision (b): confirm a redirect to a DIFFERENT host triggers a
# fresh `SafeResolver.resolve()` call - the actual connector-level
# resolution the DNS-rebinding fix depends on, not just the hostname-level
# `assert_host_is_safe()` pre-check `_fetch_safe()`'s loop also does.
# Needs a real local server + real connector (the `_patch_session` fakes
# used everywhere else in this file bypass the connector/resolver
# entirely), so `_is_unsafe_ip` is patched out for this one test only -
# loopback is what the real local TestServer binds to, and "loopback is
# blocked" is already covered by the literal-IP tests above; this test is
# solely about resolver-call wiring across a redirect, not re-proving that.
# ===========================================================================

async def test_redirect_to_different_host_triggers_a_fresh_resolver_call(monkeypatch):
    monkeypatch.setattr(network_security, "_is_unsafe_ip", lambda ip_str: False)

    async def handle_start(request: web.Request) -> web.Response:
        raise web.HTTPFound(location=request.app["final_url"])

    async def handle_final(request: web.Request) -> web.Response:
        html = (
            b"<html><body><p>Served from the second real host after the "
            b"redirect, long enough to clear the fifty character minimum."
            b"</p></body></html>"
        )
        return web.Response(body=html, content_type="text/html")

    app = web.Application()
    app.router.add_get("/start", handle_start)
    app.router.add_get("/final", handle_final)

    # Pick the port up front (rather than reading `server.port` after
    # starting) so `app["final_url"]` can be set before `start_server()` -
    # mutating app state on an already-started aiohttp `Application` is
    # deprecated.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]

    app["final_url"] = f"http://host-b.ssrf-fixture.invalid:{free_port}/final"
    server = TestServer(app, port=free_port)
    await server.start_server()

    resolved_hostnames: list[str] = []
    real_resolve = SafeResolver.resolve

    async def _spy_resolve(self, host, port=0, family=socket.AF_UNSPEC):
        resolved_hostnames.append(host)
        return await real_resolve(self, host, port, family)

    monkeypatch.setattr(SafeResolver, "resolve", _spy_resolve)

    def _fake_getaddrinfo(host, port=None, *args, **kwargs):
        if host in ("host-a.ssrf-fixture.invalid", "host-b.ssrf-fixture.invalid"):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", server.port))]
        raise socket.gaierror(f"unexpected host in test fixture: {host}")

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo)

    try:
        text = await summarize.fetch_webpage_text(
            f"http://host-a.ssrf-fixture.invalid:{server.port}/start"
        )
    finally:
        await server.close()

    assert "Served from the second real host after the redirect" in text
    assert "host-a.ssrf-fixture.invalid" in resolved_hostnames
    assert "host-b.ssrf-fixture.invalid" in resolved_hostnames
