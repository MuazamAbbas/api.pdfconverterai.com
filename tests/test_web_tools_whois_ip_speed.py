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
  - `web_tools_router.whois.whois` for WHOIS lookups.
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
the SSRF guard's own logic, matching the existing file's convention.
"""
from unittest.mock import AsyncMock

import aiohttp
import pytest
import whois
from ipwhois.exceptions import IPDefinedError

import app.routers.web_tools as web_tools_router
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

    monkeypatch.setattr(web_tools_router.whois, "whois", _fake_whois)

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

    monkeypatch.setattr(web_tools_router.whois, "whois", _fake_whois)

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

    monkeypatch.setattr(web_tools_router.whois, "whois", _fake_whois)

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

    monkeypatch.setattr(web_tools_router.whois, "whois", _fake_whois)

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

    monkeypatch.setattr(web_tools_router.whois, "whois", _boom)

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
    # `whois.whois` must never even be called once the SSRF guard rejects.
    def _fail_if_called(domain):
        raise AssertionError("whois.whois() must not be called for an unsafe host")

    monkeypatch.setattr(web_tools_router.whois, "whois", _fail_if_called)

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


# ===========================================================================
# /web_tools/ip_lookup
# ===========================================================================

class _FakeIPWhois:
    """Stands in for `ipwhois.IPWhois` - the endpoint does
    `IPWhois(raw_ip).lookup_rdap()`, so this needs a matching constructor
    signature and a `lookup_rdap()` method."""

    _rdap_result: dict | None = None
    _raise: Exception | None = None

    def __init__(self, ip):
        self.ip = ip

    def lookup_rdap(self):
        if self._raise is not None:
            raise self._raise
        return self._rdap_result


def _make_fake_ipwhois_class(rdap_result: dict | None = None, raise_exc: Exception | None = None):
    return type(
        "_FakeIPWhois", (_FakeIPWhois,), {"_rdap_result": rdap_result, "_raise": raise_exc}
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
