"""Coverage for the three new Tier 1 `web_tools` endpoints (Handbook Part
D.1 unit-test layer): `POST /web_tools/whois_lookup`, `POST /web_tools/
ip_lookup`, `POST /web_tools/speed_test` - plus the SSRF guard
(`app.shared.network_security.assert_host_is_safe`) applied to each.

Local, redis-free app fixture
------------------------------
Reuses `tests/test_web_tools_uptime_dns_ssl.py`'s `_build_web_tools_only_app()`
rather than duplicating it: `tests/conftest.py`'s shared `test_app`/`client`
fixtures call `arq.connections.create_pool(...)` against a real Redis
instance at fixture setup time, unconditionally, and Redis is not available
in this environment (see that module's docstring for the original
reproduction). None of these three endpoints ever touch
`request.app.state.arq_redis` either, so the same local, redis-free app
(mounting just `web_tools_router` + the same three global exception
handlers `app/main.py` registers, with `app.state.arq_redis` set to a plain
`AsyncMock`) is reused here via import rather than copy-paste.

Mocking convention
-------------------
Monkeypatches the router module's own call sites rather than reaching into
library internals - no real outbound network calls anywhere in this file:
  - `web_tools_router._safe_whois_lookup` for WHOIS lookups (a local
    wrapper the router calls instead of `whois.whois()` directly - see
    `_SafeNICClient`'s docstring in `web_tools.py` for why).
  - `web_tools_router.IPWhois` (the class itself, bound directly into the
    router's namespace via `from ipwhois import IPWhois`) for IP lookups.
  - `web_tools_router.aiohttp.ClientSession` for the speed test - `speed_test()`
    constructs its own `aiohttp.ClientSession(...)` inline (no injected
    `session` parameter like `check_url()` has), so there is no narrower
    seam than the module-level `aiohttp.ClientSession` reference itself.
    This mirrors the exact pattern the existing file already established for
    the same reason in `test_website_down_detector_429_exhausted_retries_
    degrades_gracefully` (`_ScriptedSessionFactory` wrapping
    `web_tools_router.aiohttp.ClientSession`) - not a new gap.

The SSRF-target addresses are exercised through the *real*, unmocked
`assert_host_is_safe()` - `socket.getaddrinfo()` resolves an IP-literal to
itself with no network I/O, so this is a real (not simulated) exercise of
the SSRF guard's own logic, matching the existing file's convention. The one
exception is `test_whois_lookup_referral_to_unsafe_host_is_blocked` below,
which deliberately reaches deeper - into `web_tools_router._SafeWhoisSocket`
specifically (this fix's own new socket subclass, not a shared/global one -
see that test's own docstring for why patching the *global* `socket.socket`
class instead broke the test run entirely) - to exercise the real WHOIS
referral/recursion flow and the real `assert_host_is_safe_sync()` guard
end-to-end.
"""
import time
from unittest.mock import AsyncMock

import aiohttp
import pytest
import whois
from ipwhois.exceptions import HTTPLookupError, IPDefinedError

import app.routers.web_tools as web_tools_router
from app.shared.network_security import assert_host_is_safe_sync
from tests.test_web_tools_uptime_dns_ssl import _build_web_tools_only_app

pytestmark = pytest.mark.asyncio(loop_scope="session")

# Matches `test_web_tools_uptime_dns_ssl.SSRF_TARGETS` - kept as a local copy
# (rather than importing it) since these tests format some of these targets
# into different fields (`domain`, `ip`, `url`) than that file does.
SSRF_TARGETS = ["127.0.0.1", "169.254.169.254", "10.0.0.5", "192.168.1.1", "::1"]


@pytest.fixture
def web_tools_app():
    app = _build_web_tools_only_app()
    # These 3 endpoints never touch arq_redis - a plain AsyncMock stand-in
    # sidesteps the real-Redis requirement entirely (see module docstring).
    app.state.arq_redis = AsyncMock()
    return app


@pytest.fixture
async def web_client(web_tools_app):
    from httpx import ASGITransport, AsyncClient

    transport = ASGITransport(app=web_tools_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ===========================================================================
# /web_tools/whois_lookup
# ===========================================================================

async def test_whois_lookup_happy_path(web_client, api_key, monkeypatch):
    import datetime as dt

    creation = dt.datetime(2000, 1, 1, tzinfo=dt.timezone.utc)
    expiration = dt.datetime(2030, 1, 1, tzinfo=dt.timezone.utc)

    def _fake_whois(domain):
        return {
            "registrar": "Example Registrar, Inc.",
            "creation_date": creation,
            "expiration_date": expiration,
            "name_servers": ["NS1.EXAMPLE.COM", "ns2.example.com.", "ns1.example.com"],
            "org": "Example Org",
        }

    monkeypatch.setattr(web_tools_router, "_safe_whois_lookup", _fake_whois)

    resp = await web_client.post(
        "/v1/web_tools/whois_lookup",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["domain"] == "example.com"
    assert data["registrar"] == "Example Registrar, Inc."
    assert data["creation_date"] == creation.isoformat()
    assert data["expiration_date"] == expiration.isoformat()
    # Deduplicated, lowercased, trailing-dot-stripped, order preserved.
    assert data["name_servers"] == ["ns1.example.com", "ns2.example.com"]
    assert data["registrant_org"] == "Example Org"
    assert data["error"] is None


async def test_whois_lookup_tolerates_full_url_and_list_creation_date(
    web_client, api_key, monkeypatch
):
    import datetime as dt

    early = dt.datetime(1999, 6, 1, tzinfo=dt.timezone.utc)
    late = dt.datetime(1999, 8, 1, tzinfo=dt.timezone.utc)
    seen_domains = []

    def _fake_whois(domain):
        seen_domains.append(domain)
        return {
            "registrar": "Example Registrar",
            "creation_date": [late, early],  # earliest must win
            "expiration_date": None,
            "name_servers": "single-ns.example.com",  # bare string, not a list
            "org": None,
        }

    monkeypatch.setattr(web_tools_router, "_safe_whois_lookup", _fake_whois)

    resp = await web_client.post(
        "/v1/web_tools/whois_lookup",
        json={"domain": "https://example.com/some/path"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["domain"] == "example.com"
    assert seen_domains == ["example.com"]
    assert data["creation_date"] == early.isoformat()
    assert data["name_servers"] == ["single-ns.example.com"]
    # `org` is None (GDPR-redacted) - a legitimate result, not "no data".
    assert data["registrant_org"] is None
    assert data["error"] is None


async def test_whois_lookup_pywhois_error_is_clean_no_data_not_a_crash(
    web_client, api_key, monkeypatch
):
    def _fake_whois(domain):
        raise whois.exceptions.PywhoisError("No match for domain.")

    monkeypatch.setattr(web_tools_router, "_safe_whois_lookup", _fake_whois)

    resp = await web_client.post(
        "/v1/web_tools/whois_lookup",
        json={"domain": "this-domain-does-not-exist-at-all.invalid"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["registrar"] is None
    assert data["creation_date"] is None
    assert data["expiration_date"] is None
    assert data["name_servers"] == []
    assert data["registrant_org"] is None
    assert data["error"] == "No WHOIS data found for this domain"


async def test_whois_lookup_empty_record_is_clean_no_data_not_a_crash(
    web_client, api_key, monkeypatch
):
    """The library can also return a parsed object with every field `None`
    instead of raising - same "no data" outcome, different shape."""
    def _fake_whois(domain):
        return {
            "registrar": None,
            "creation_date": None,
            "expiration_date": None,
            "name_servers": None,
            "org": None,
        }

    monkeypatch.setattr(web_tools_router, "_safe_whois_lookup", _fake_whois)

    resp = await web_client.post(
        "/v1/web_tools/whois_lookup",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["name_servers"] == []
    assert data["error"] == "No WHOIS data found for this domain"


async def test_whois_lookup_missing_domain_is_400(web_client, api_key):
    resp = await web_client.post(
        "/v1/web_tools/whois_lookup",
        json={"domain": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "DOMAIN_REQUIRED"


async def test_whois_lookup_generic_exception_returns_500_without_leak(
    web_client, api_key, monkeypatch
):
    def _boom(domain):
        raise RuntimeError("SECRET_MARKER_whois db_password=hunter2 at web_tools.py")

    monkeypatch.setattr(web_tools_router, "_safe_whois_lookup", _boom)

    resp = await web_client.post(
        "/v1/web_tools/whois_lookup",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "WHOIS_LOOKUP_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "RuntimeError" not in resp.text


@pytest.mark.parametrize("target", SSRF_TARGETS)
async def test_whois_lookup_blocks_ssrf_targets(web_client, api_key, monkeypatch, target):
    # `_safe_whois_lookup` must never even be called once the pre-lookup
    # SSRF guard rejects the domain itself.
    def _fail_if_called(domain):
        raise AssertionError("_safe_whois_lookup() must not be called for an unsafe host")

    monkeypatch.setattr(web_tools_router, "_safe_whois_lookup", _fail_if_called)

    domain = f"[{target}]" if ":" in target else target
    resp = await web_client.post(
        "/v1/web_tools/whois_lookup",
        json={"domain": domain},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["name_servers"] == []
    assert data["error"] == "Cannot check internal or reserved network addresses"


async def test_whois_lookup_registrant_org_only_is_not_classified_as_no_data(
    web_client, api_key, monkeypatch
):
    """Regression test for the `registrant_org`-only classification gap:
    `registrant_org` is deliberately excluded from the "has data" check
    (GDPR-redaction rationale - its *absence* shouldn't disqualify a
    record that has everything else), but that must not mean a record
    where it's the *only* populated field gets silently discarded as "no
    WHOIS data found"."""
    def _fake_whois(domain):
        return {
            "registrar": None,
            "creation_date": None,
            "expiration_date": None,
            "name_servers": None,
            "org": "Some Registrant Org",
        }

    monkeypatch.setattr(web_tools_router, "_safe_whois_lookup", _fake_whois)

    resp = await web_client.post(
        "/v1/web_tools/whois_lookup",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["registrant_org"] == "Some Registrant Org"
    assert data["error"] is None


async def test_whois_lookup_referral_to_unsafe_host_is_blocked(
    web_client, api_key, monkeypatch
):
    """Regression test for the WHOIS referral SSRF gap (finding #1):
    `whois.whois()` never actually connects to the caller-supplied
    domain's own address - it connects to a TLD-mapped whois host chosen
    internally by the library, then (by default) regex-extracts a
    "Whois Server: <host>" value straight out of that response and opens a
    second, raw `socket.connect((nhost, 43))` to it with no validation at
    all (see `NICClient.whois()` / `findwhois_server()` in
    `.venv/Lib/site-packages/whois/whois.py`). The pre-lookup
    `assert_host_is_safe(domain)` check in `whois_lookup()` never sees
    this second hop, since it only ever validates the original domain.

    This can't be exercised by mocking `_safe_whois_lookup` itself (that
    would just prove the mock returns what it's told to, not that the real
    guard fires) or by hitting a real WHOIS server (no real outbound
    network calls in this test file). Instead, the transport is scripted
    to simulate two hops without any actual network I/O, while the real
    `NICClient` recursion (`findwhois_server()`'s regex-extraction of the
    referral host, the recursive `self.whois()` call) and the real
    `assert_host_is_safe_sync()` guard both run unmocked for both hops.

    Scoped to `web_tools_router._SafeWhoisSocket` specifically - NOT the
    base `socket.socket` class - deliberately: an earlier version of this
    test patched `socket.socket.connect/send/recv` globally and caused the
    whole test (and sometimes the whole run) to hang. Root cause: on this
    platform `asyncio`'s event loop wakes worker threads' completions
    (`asyncio.to_thread()`, used by the endpoint under test) via an
    internal loopback "self-pipe" socket, and a process-wide monkeypatch
    of `socket.socket.send`/`recv` intercepts that self-pipe's own traffic
    right along with the WHOIS socket's - the loop's wakeup notification
    silently vanishes into the fake instead of the real self-pipe, and the
    `await` never resolves. `_SafeWhoisSocket` is a distinct subclass used
    only by this WHOIS code path, so patching it directly reaches none of
    that shared machinery. `connect()` itself is faked here (rather than
    left to actually call the real `assert_host_is_safe_sync()` +
    `super().connect()` and only faking the transport underneath) for the
    same reason - it still calls the real, unmocked `assert_host_is_safe_sync()`
    guard function itself, just without going through `super().connect()`
    into the shared base-socket layer at all.
    """
    first_hop_host = "8.8.8.8"  # a real, public, safe IP literal.
    referral_host = "127.0.0.1"
    first_hop_response = (
        f"Domain Name: EXAMPLE.COM\r\nWhois Server: {referral_host}\r\n"
    ).encode()

    connected_hosts: list[str] = []

    def _fake_connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        # The real guard, unmocked - this is what's actually being tested.
        assert_host_is_safe_sync(host)
        connected_hosts.append(host)
        self._fake_connected_host = host
        return None

    def _fake_send(self, data):
        return len(data)

    def _fake_recv(self, bufsize):
        # NICClient.whois()'s `while True: recv(...)` loop stops once
        # `recv()` returns empty bytes - one scripted chunk, then EOF.
        if getattr(self, "_fake_recv_done", False):
            return b""
        self._fake_recv_done = True
        if getattr(self, "_fake_connected_host", None) == first_hop_host:
            return first_hop_response
        return b""

    monkeypatch.setattr(web_tools_router._SafeWhoisSocket, "connect", _fake_connect)
    monkeypatch.setattr(web_tools_router._SafeWhoisSocket, "send", _fake_send)
    monkeypatch.setattr(web_tools_router._SafeWhoisSocket, "recv", _fake_recv)
    monkeypatch.setattr(
        web_tools_router._SafeNICClient, "choose_server", lambda self, domain: first_hop_host
    )

    resp = await web_client.post(
        "/v1/web_tools/whois_lookup",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["error"] == "Cannot check internal or reserved network addresses"
    # The real fix: the referral host was never actually connected to -
    # `assert_host_is_safe_sync()` raised for it before it was ever
    # appended to `connected_hosts`.
    assert connected_hosts == [first_hop_host]


# ===========================================================================
# /web_tools/ip_lookup
# ===========================================================================

class _FakeIPWhois:
    """Stands in for `ipwhois.IPWhois` - the endpoint does
    `IPWhois(raw_ip).lookup_rdap(retry_count=..., rate_limit_timeout=...)`,
    so this needs a matching constructor signature and a `lookup_rdap()`
    method that accepts (and can optionally record) those kwargs."""

    _rdap_result: dict | None = None
    _raise: Exception | None = None
    _sleep_seconds: float = 0.0
    _received_kwargs_sink: list | None = None

    def __init__(self, ip):
        self.ip = ip

    def lookup_rdap(self, **kwargs):
        if self._received_kwargs_sink is not None:
            self._received_kwargs_sink.append(kwargs)
        if self._sleep_seconds:
            # Blocking sleep - runs inside `asyncio.to_thread()` on a real
            # worker thread in production, so this is a faithful stand-in
            # for "the library call itself blocks for a while", used to
            # exercise the `asyncio.wait_for()` backstop (finding #2).
            time.sleep(self._sleep_seconds)
        if self._raise is not None:
            raise self._raise
        return self._rdap_result


def _make_fake_ipwhois_class(
    rdap_result: dict | None = None,
    raise_exc: Exception | None = None,
    sleep_seconds: float = 0.0,
    received_kwargs_sink: list | None = None,
):
    return type(
        "_FakeIPWhois",
        (_FakeIPWhois,),
        {
            "_rdap_result": rdap_result,
            "_raise": raise_exc,
            "_sleep_seconds": sleep_seconds,
            "_received_kwargs_sink": received_kwargs_sink,
        },
    )


async def test_ip_lookup_happy_path(web_client, api_key, monkeypatch):
    fake_result = {
        "asn": "15169",
        "asn_description": "GOOGLE, US",
        "asn_country_code": "US",
        "network": {"name": "GOOGLE", "cidr": "8.8.8.0/24"},
        "objects": {
            "abuse-handle": {
                "roles": ["abuse"],
                "contact": {"email": [{"value": "network-abuse@google.com"}]},
            },
            "other-handle": {"roles": ["technical"], "contact": {"email": [{"value": "tech@google.com"}]}},
        },
    }
    monkeypatch.setattr(web_tools_router, "IPWhois", _make_fake_ipwhois_class(fake_result))

    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": "8.8.8.8"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["ip"] == "8.8.8.8"
    assert data["asn"] == "15169"
    assert data["asn_description"] == "GOOGLE, US"
    assert data["network_name"] == "GOOGLE"
    assert data["network_cidr"] == "8.8.8.0/24"
    assert data["country"] == "US"
    assert data["abuse_contact"] == "network-abuse@google.com"
    assert data["error"] is None


async def test_ip_lookup_no_abuse_contact_is_none_not_an_error(web_client, api_key, monkeypatch):
    fake_result = {
        "asn": "15169",
        "asn_description": "GOOGLE, US",
        "asn_country_code": "US",
        "network": {"name": "GOOGLE", "cidr": "8.8.8.0/24"},
        "objects": None,
    }
    monkeypatch.setattr(web_tools_router, "IPWhois", _make_fake_ipwhois_class(fake_result))

    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": "8.8.8.8"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["abuse_contact"] is None
    assert data["error"] is None


async def test_ip_lookup_defined_error_is_clean_no_data_not_a_crash(
    web_client, api_key, monkeypatch
):
    """`IPDefinedError` is `ipwhois`'s own "nothing here" signal for
    special-use addresses it recognizes - must degrade cleanly, not crash."""
    monkeypatch.setattr(
        web_tools_router, "IPWhois", _make_fake_ipwhois_class(raise_exc=IPDefinedError("reserved"))
    )

    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": "8.8.8.8"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["asn"] is None
    assert data["error"] == "No ownership data available for this address"


async def test_ip_lookup_uses_tight_retry_and_rate_limit_bounds(web_client, api_key, monkeypatch):
    """Regression test for finding #2: `lookup_rdap()` must be called with
    tight, explicit `retry_count`/`rate_limit_timeout` bounds, not the
    library's own defaults (`retry_count=3`, `rate_limit_timeout=120`) -
    on a rate-limited RIR RDAP server those defaults can block a
    thread-pool worker for minutes (`get_http_json()` sleeps up to
    `rate_limit_timeout` seconds before each retry)."""
    received: list[dict] = []
    fake_result = {
        "asn": "15169", "asn_description": None, "asn_country_code": None,
        "network": {}, "objects": None,
    }
    monkeypatch.setattr(
        web_tools_router,
        "IPWhois",
        _make_fake_ipwhois_class(rdap_result=fake_result, received_kwargs_sink=received),
    )

    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": "8.8.8.8"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert len(received) == 1
    assert received[0]["retry_count"] == web_tools_router._IP_LOOKUP_RETRY_COUNT
    assert received[0]["rate_limit_timeout"] == web_tools_router._IP_LOOKUP_RATE_LIMIT_TIMEOUT
    # The whole point: meaningfully tighter than the library's own defaults.
    assert web_tools_router._IP_LOOKUP_RETRY_COUNT < 3
    assert web_tools_router._IP_LOOKUP_RATE_LIMIT_TIMEOUT < 120


async def test_ip_lookup_hard_timeout_backstop_degrades_cleanly(web_client, api_key, monkeypatch):
    """Regression test for finding #2's `asyncio.wait_for()` hard backstop:
    even if the underlying call somehow blocks past the tight retry/
    rate-limit bounds above, the endpoint must still return a clean
    degrade instead of hanging the request indefinitely."""
    monkeypatch.setattr(web_tools_router, "_IP_LOOKUP_TOTAL_TIMEOUT", 0.05)
    monkeypatch.setattr(
        web_tools_router,
        "IPWhois",
        _make_fake_ipwhois_class(rdap_result={"asn": "1"}, sleep_seconds=0.5),
    )

    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": "8.8.8.8"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["asn"] is None
    assert data["error"] == "IP lookup timed out, please try again later"


async def test_ip_lookup_rdap_network_failure_is_clean_degrade_not_a_crash(
    web_client, api_key, monkeypatch
):
    """Regression test for finding #3: a transient RDAP/ASN network failure
    (rate limit exhausted, HTTP lookup failed, etc. - the exception types
    `ipwhois` itself raises, under `ipwhois.exceptions.BaseIpwhoisException`)
    must degrade cleanly like `IPDefinedError` already does above, not
    surface as a scary 500 - matching how `check_url()`/`ssl_checker()`
    elsewhere in this file treat their own connection-level failures as
    legitimate check results, not internal errors."""
    monkeypatch.setattr(
        web_tools_router,
        "IPWhois",
        _make_fake_ipwhois_class(
            raise_exc=HTTPLookupError(
                "HTTP lookup failed for https://rdap.example/ip/8.8.8.8."
            )
        ),
    )

    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": "8.8.8.8"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["asn"] is None
    assert data["error"] == "Unable to complete IP lookup right now, please try again later"


async def test_ip_lookup_missing_ip_is_400(web_client, api_key):
    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "IP_REQUIRED"


async def test_ip_lookup_invalid_syntax_is_400(web_client, api_key):
    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": "not-an-ip-address"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "IP_INVALID"


async def test_ip_lookup_generic_exception_returns_500_without_leak(web_client, api_key, monkeypatch):
    monkeypatch.setattr(
        web_tools_router,
        "IPWhois",
        _make_fake_ipwhois_class(
            raise_exc=RuntimeError("SECRET_MARKER_ip db_password=hunter2 at web_tools.py")
        ),
    )

    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": "8.8.8.8"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "IP_LOOKUP_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "RuntimeError" not in resp.text


@pytest.mark.parametrize("target", ["127.0.0.1", "169.254.169.254"])
async def test_ip_lookup_blocks_ssrf_targets(web_client, api_key, monkeypatch, target):
    def _fail_if_called(ip):
        raise AssertionError("IPWhois() must not be called for an unsafe address")

    monkeypatch.setattr(web_tools_router, "IPWhois", _fail_if_called)

    resp = await web_client.post(
        "/v1/web_tools/ip_lookup",
        json={"ip": target},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["ip"] == target
    assert data["asn"] is None
    assert data["error"] == "Cannot check internal or reserved network addresses"


# ===========================================================================
# /web_tools/speed_test
# ===========================================================================

class _FakeSpeedResponse:
    """Mimics just enough of `aiohttp.ClientResponse` for `speed_test()`:
    `.status`, `.headers.get("Location")`, async `.read()`, and
    async-context-manager support (`async with session.get(...) as response:`).
    """

    def __init__(self, status: int, location: str | None = None, body: bytes = b""):
        self.status = status
        self.headers = {"Location": location} if location else {}
        self._body = body

    async def read(self) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _ScriptedSpeedSession:
    """Fake `aiohttp.ClientSession` instance: serves one scripted
    `_FakeSpeedResponse` (or a list of them, popped in order) per URL,
    records every URL requested, and supports the
    `async with session.get(url, allow_redirects=False) as response:` shape
    `speed_test()` uses."""

    def __init__(self, responses_by_url=None, raise_on_get: Exception | None = None):
        self._responses = responses_by_url or {}
        self._raise_on_get = raise_on_get
        self.requested_urls: list[str] = []

    def get(self, url, allow_redirects=False):
        assert allow_redirects is False, (
            "speed_test() must disable aiohttp's own redirect-following so "
            "each hop can be SSRF-checked, matching check_url()'s convention"
        )
        self.requested_urls.append(url)
        if self._raise_on_get is not None:
            raise self._raise_on_get
        entry = self._responses[url]
        if isinstance(entry, list):
            return entry.pop(0)
        return entry

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _ScriptedSpeedSessionFactory:
    """Stands in for `aiohttp.ClientSession` itself (the class) - `speed_test()`
    does `async with aiohttp.ClientSession(trace_configs=..., timeout=...) as
    session:`, constructing its own session inline rather than taking one as
    a parameter (unlike `check_url()`), so this needs to be both callable
    (the constructor call, accepting/ignoring the trace_configs/timeout
    kwargs) and an async context manager yielding the pre-built scripted
    session. Mirrors `test_web_tools_uptime_dns_ssl.py`'s
    `_ScriptedSessionFactory`, which does the same for `check_url()`'s
    regression tests."""

    def __init__(self, session):
        self._session = session

    def __call__(self, *args, **kwargs):
        return self

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, *exc_info):
        return False


def _patch_speed_session(monkeypatch, session_or_exc):
    if isinstance(session_or_exc, Exception):
        def _raise_constructor(*args, **kwargs):
            raise session_or_exc

        monkeypatch.setattr(web_tools_router.aiohttp, "ClientSession", _raise_constructor)
    else:
        monkeypatch.setattr(
            web_tools_router.aiohttp, "ClientSession", _ScriptedSpeedSessionFactory(session_or_exc)
        )


async def test_speed_test_happy_path(web_client, api_key, monkeypatch):
    session = _ScriptedSpeedSession({
        "https://example.com": _FakeSpeedResponse(200, body=b"hello world"),
    })
    _patch_speed_session(monkeypatch, session)

    resp = await web_client.post(
        "/v1/web_tools/speed_test",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["url"] == "https://example.com"
    assert data["status_code"] == 200
    assert data["content_size_bytes"] == len(b"hello world")
    assert isinstance(data["total_time_ms"], (int, float))
    assert data["error"] is None
    assert session.requested_urls == ["https://example.com"]


async def test_speed_test_follows_redirect_then_succeeds(web_client, api_key, monkeypatch):
    session = _ScriptedSpeedSession({
        "https://example.com": _FakeSpeedResponse(301, location="https://example.com/"),
        "https://example.com/": _FakeSpeedResponse(200, body=b"ok"),
    })
    _patch_speed_session(monkeypatch, session)

    resp = await web_client.post(
        "/v1/web_tools/speed_test",
        json={"url": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status_code"] == 200
    assert data["content_size_bytes"] == 2
    assert data["error"] is None
    assert session.requested_urls == ["https://example.com", "https://example.com/"]


async def test_speed_test_redirect_loop_is_clean_degrade_not_a_crash(web_client, api_key, monkeypatch):
    session = _ScriptedSpeedSession({
        "https://example.com": [
            _FakeSpeedResponse(302, location="https://example.com") for _ in range(10)
        ],
    })
    _patch_speed_session(monkeypatch, session)

    resp = await web_client.post(
        "/v1/web_tools/speed_test",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status_code"] is None
    assert data["error"] == "Too many redirects"
    assert len(session.requested_urls) == web_tools_router._MAX_REDIRECT_HOPS + 1


async def test_speed_test_connector_error_is_clean_degrade_not_a_crash(web_client, api_key, monkeypatch):
    session = _ScriptedSpeedSession(
        raise_on_get=aiohttp.ClientConnectorError(
            connection_key=None, os_error=OSError("SECRET_MARKER_speed db_password=hunter2")
        )
    )
    _patch_speed_session(monkeypatch, session)

    resp = await web_client.post(
        "/v1/web_tools/speed_test",
        json={"url": "https://totally-unreachable.invalid"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status_code"] is None
    assert data["error"] == "Unable to establish a TLS connection on port 443"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text


async def test_speed_test_missing_url_is_400(web_client, api_key):
    resp = await web_client.post(
        "/v1/web_tools/speed_test",
        json={"url": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "URL_REQUIRED"


async def test_speed_test_invalid_url_format_is_400(web_client, api_key):
    resp = await web_client.post(
        "/v1/web_tools/speed_test",
        json={"url": "http://"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "URL_INVALID"


async def test_speed_test_generic_exception_returns_500_without_leak(web_client, api_key, monkeypatch):
    _patch_speed_session(
        monkeypatch, RuntimeError("SECRET_MARKER_speed2 db_password=hunter2 at web_tools.py")
    )

    resp = await web_client.post(
        "/v1/web_tools/speed_test",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "SPEED_TEST_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "RuntimeError" not in resp.text


@pytest.mark.parametrize("target", ["127.0.0.1", "169.254.169.254"])
async def test_speed_test_blocks_ssrf_targets_pre_request(web_client, api_key, monkeypatch, target):
    # aiohttp.ClientSession must never even be constructed for an unsafe host.
    def _fail_if_called(*args, **kwargs):
        raise AssertionError("aiohttp.ClientSession() must not be constructed for an unsafe host")

    monkeypatch.setattr(web_tools_router.aiohttp, "ClientSession", _fail_if_called)

    resp = await web_client.post(
        "/v1/web_tools/speed_test",
        json={"url": f"http://{target}/"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["status_code"] is None
    assert data["total_time_ms"] is None
    assert data["error"] == "Cannot check internal or reserved network addresses"


async def test_speed_test_blocks_ssrf_on_redirect_hop(web_client, api_key, monkeypatch):
    """The pre-request guard only checks the original hostname - a redirect
    hop that targets an internal address must also be rejected cleanly, the
    same way `check_url()`'s redirect-hop guard already is."""
    session = _ScriptedSpeedSession({
        "https://example.com": _FakeSpeedResponse(302, location="http://169.254.169.254/latest/meta-data/"),
    })
    _patch_speed_session(monkeypatch, session)

    resp = await web_client.post(
        "/v1/web_tools/speed_test",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["status_code"] is None
    assert data["error"] == "Cannot check internal or reserved network addresses"
    # The unsafe redirect target was never actually followed/requested again.
    assert session.requested_urls == ["https://example.com"]
