"""Coverage for the three new Tier 1 `web_tools` endpoints (Handbook Part
D.1 unit-test layer): `POST /web_tools/website_down_detector`,
`POST /web_tools/dns_lookup`, `POST /web_tools/ssl_checker` - and the SSRF
guard (`app.shared.network_security.assert_host_is_safe`) retrofitted onto
`check_url()`/these three new endpoints.

Local, redis-free app fixture
------------------------------
`tests/conftest.py`'s shared `test_app`/`client` fixtures call
`arq.connections.create_pool(...)` against a real Redis instance
(`redis://localhost:6379`) at fixture setup time, unconditionally - Redis is
not available in this environment (confirmed: `redis-server`/`redis-cli` are
not on PATH, nothing listens on 6379, and Docker Desktop's daemon isn't
running here either), and every test in `tests/test_web_tools_robustness.py`
that uses those shared fixtures currently fails with
`redis.exceptions.TimeoutError` at setup (reproduced directly - see the test
run in the task report, not asserted from memory).

None of the three endpoints under test here ever touch
`request.app.state.arq_redis` (only `/web_tools/summarize` -> `_create_web_
tools_job` does, and that's out of scope for this file), so instead of
depending on real Redis, this file builds its own minimal app that mounts
just `web_tools_router` plus the same three global exception handlers
`app/main.py`/`tests/conftest.py` register, and sets `app.state.arq_redis`
to a plain `AsyncMock` stand-in that is simply never called by these routes.
This is a legitimate test seam (these are genuinely Tier 1 sync endpoints
with no queue dependency), not a weakened test - flagged in the task report
per the brief's instruction to note the Redis-unavailability finding
explicitly rather than silently skip.

Mocking convention
-------------------
Follows the same idiom as `tests/test_web_tools_robustness.py`: monkeypatch
the router module's own functions (`web_tools_router.check_url`,
`web_tools_router._query_dns_record`, `web_tools_router._fetch_certificate_
der`) rather than reaching into `aiohttp`/`dns`/`socket+ssl` internals - no
real outbound network calls are made anywhere in this file.

The five SSRF-target addresses (`127.0.0.1`, `169.254.169.254`, `10.0.0.5`,
`192.168.1.1`, `::1`) are exercised through the *real*, unmocked
`assert_host_is_safe()` - `socket.getaddrinfo()` resolves an IP-literal to
itself with no network I/O, so this is a real (not simulated) exercise of
the SSRF guard's own logic, not a stand-in for it.
"""
import datetime as dt
from unittest.mock import AsyncMock

import aiohttp
import pytest
import tenacity
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from httpx import ASGITransport, AsyncClient
from starlette.exceptions import HTTPException as StarletteHTTPException

import app.routers.web_tools as web_tools_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

SSRF_TARGETS = ["127.0.0.1", "169.254.169.254", "10.0.0.5", "192.168.1.1", "::1"]


# ---------------------------------------------------------------------------
# Local, redis-free app/client fixtures (see module docstring)
# ---------------------------------------------------------------------------

def _build_web_tools_only_app() -> FastAPI:
    app = FastAPI()
    app.include_router(web_tools_router.router, prefix="/v1")

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
            content={
                "success": False, "message": "Invalid request",
                "error": {"code": "VALIDATION_ERROR"},
            },
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception):
        return JSONResponse(
            status_code=500,
            content={
                "success": False, "message": "Internal server error",
                "error": {"code": "INTERNAL_ERROR"},
            },
        )

    return app


@pytest.fixture
def web_tools_app():
    app = _build_web_tools_only_app()
    # These 3 endpoints never touch arq_redis - a plain AsyncMock stand-in
    # sidesteps the real-Redis requirement entirely (see module docstring).
    app.state.arq_redis = AsyncMock()
    return app


@pytest.fixture
async def web_client(web_tools_app):
    transport = ASGITransport(app=web_tools_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ---------------------------------------------------------------------------
# Certificate fixtures - real, freshly generated x509 certs (DER bytes), so
# the endpoint's actual `x509.load_der_x509_certificate` parsing path is
# exercised for real rather than mocked away.
# ---------------------------------------------------------------------------

def _make_cert_der(
    common_name: str,
    not_before: dt.datetime,
    not_after: dt.datetime,
    issuer_cn: str | None = None,
) -> bytes:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, issuer_cn or common_name)])
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(encoding=serialization.Encoding.DER)


@pytest.fixture
def valid_cert_der():
    now = dt.datetime.now(dt.timezone.utc)
    return _make_cert_der("example.com", now - dt.timedelta(days=1), now + dt.timedelta(days=60))


@pytest.fixture
def expired_cert_der():
    now = dt.datetime.now(dt.timezone.utc)
    return _make_cert_der("example.com", now - dt.timedelta(days=400), now - dt.timedelta(days=30))


@pytest.fixture
def self_signed_cert_der():
    now = dt.datetime.now(dt.timezone.utc)
    # issuer == subject by construction (no issuer_cn override) - a genuine
    # self-signed cert, not just a mocked flag.
    return _make_cert_der(
        "selfsigned.example.com", now - dt.timedelta(days=1), now + dt.timedelta(days=60)
    )


@pytest.fixture
def mismatch_cert_der():
    now = dt.datetime.now(dt.timezone.utc)
    return _make_cert_der(
        "wrong-name.example.net", now - dt.timedelta(days=1), now + dt.timedelta(days=60),
        issuer_cn="Some CA",
    )


# ===========================================================================
# /web_tools/website_down_detector
# ===========================================================================

async def test_website_down_detector_happy_path_up(web_client, api_key, monkeypatch):
    async def _fake_check_url(session, url):
        return True, 200

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await web_client.post(
        "/v1/web_tools/website_down_detector",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["url"] == "https://example.com"
    assert data["is_up"] is True
    assert data["status_code"] == 200
    assert isinstance(data["response_time_ms"], (int, float))
    assert data["checked_at"]
    assert data["error"] is None


async def test_website_down_detector_non_success_status_is_down(web_client, api_key, monkeypatch):
    async def _fake_check_url(session, url):
        return False, 404

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await web_client.post(
        "/v1/web_tools/website_down_detector",
        json={"url": "https://example.com/missing"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_up"] is False
    assert data["status_code"] == 404
    assert data["error"] == "Website returned a non-success status code"


async def test_website_down_detector_client_response_error_is_down(
    web_client, api_key, monkeypatch
):
    import aiohttp

    async def _fake_check_url(session, url):
        request_info = aiohttp.RequestInfo(
            url="http://example.com", method="GET", headers={}, real_url="http://example.com",
        )
        raise aiohttp.ClientResponseError(
            request_info=request_info, history=(), status=429,
            message="SECRET_MARKER db_password=hunter2",
        )

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await web_client.post(
        "/v1/web_tools/website_down_detector",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_up"] is False
    assert data["status_code"] == 429
    assert data["error"] == "The website returned an error response"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text


async def test_website_down_detector_timeout_is_down(web_client, api_key, monkeypatch):
    import asyncio

    async def _fake_check_url(session, url):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await web_client.post(
        "/v1/web_tools/website_down_detector",
        json={"url": "https://slow.example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_up"] is False
    assert data["status_code"] is None
    assert data["error"] == "Website did not respond in time"


async def test_website_down_detector_unreachable_host_is_down(web_client, api_key, monkeypatch):
    import aiohttp

    async def _fake_check_url(session, url):
        raise aiohttp.ClientConnectorError(
            connection_key=None, os_error=OSError("SECRET_MARKER db_password=hunter2")
        )

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await web_client.post(
        "/v1/web_tools/website_down_detector",
        json={"url": "https://totally-unreachable.invalid"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_up"] is False
    assert data["status_code"] is None
    assert data["error"] == "Website is unreachable"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text


async def test_website_down_detector_generic_exception_returns_500_without_leak(
    web_client, api_key, monkeypatch
):
    async def _fake_check_url(session, url):
        raise RuntimeError("SECRET_MARKER_never_leak db_password=hunter2 at web_tools.py")

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await web_client.post(
        "/v1/web_tools/website_down_detector",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "WEBSITE_DOWN_DETECTOR_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "RuntimeError" not in resp.text


async def test_website_down_detector_missing_url_is_400(web_client, api_key):
    resp = await web_client.post(
        "/v1/web_tools/website_down_detector",
        json={"url": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "URL_REQUIRED"


@pytest.mark.parametrize("target", SSRF_TARGETS)
async def test_website_down_detector_blocks_ssrf_targets(web_client, api_key, target):
    url = f"http://[{target}]" if ":" in target else f"http://{target}"
    resp = await web_client.post(
        "/v1/web_tools/website_down_detector",
        json={"url": url},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["is_up"] is False
    assert data["status_code"] is None
    assert data["error"] == "Cannot check internal or reserved network addresses"


# ===========================================================================
# /web_tools/dns_lookup
# ===========================================================================

async def test_dns_lookup_happy_path_returns_records_per_type(web_client, api_key, monkeypatch):
    fake_records = {
        "A": ["93.184.216.34"],
        "AAAA": ["2606:2800:220:1:248:1893:25c8:1946"],
        "MX": [],  # no MX records is normal, not an error
        "TXT": ["v=spf1 -all"],
        "NS": ["a.iana-servers.net", "b.iana-servers.net"],
        "CNAME": [],
    }

    async def _fake_query(resolver, domain, rtype):
        return fake_records[rtype], False

    monkeypatch.setattr(web_tools_router, "_query_dns_record", _fake_query)

    resp = await web_client.post(
        "/v1/web_tools/dns_lookup",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["domain"] == "example.com"
    assert data["records"] == fake_records
    assert data["error"] is None


async def test_dns_lookup_tolerates_full_url_in_domain_field(web_client, api_key, monkeypatch):
    seen_domains = []

    async def _fake_query(resolver, domain, rtype):
        seen_domains.append(domain)
        return [], False

    monkeypatch.setattr(web_tools_router, "_query_dns_record", _fake_query)

    resp = await web_client.post(
        "/v1/web_tools/dns_lookup",
        json={"domain": "https://example.com/some/path?x=1"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["domain"] == "example.com"
    assert all(d == "example.com" for d in seen_domains)


async def test_dns_lookup_nxdomain_sets_top_level_error_and_empty_records(
    web_client, api_key, monkeypatch
):
    calls = {"n": 0}

    async def _fake_query(resolver, domain, rtype):
        calls["n"] += 1
        if rtype == "A":
            return [], True  # NXDOMAIN on the first record type queried
        return ["should not be reached"], False

    monkeypatch.setattr(web_tools_router, "_query_dns_record", _fake_query)

    resp = await web_client.post(
        "/v1/web_tools/dns_lookup",
        json={"domain": "this-domain-does-not-exist-at-all.invalid"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["error"] == "Domain does not exist"
    assert data["records"] == {t: [] for t in web_tools_router.DNS_RECORD_TYPES}
    # Short-circuits on NXDOMAIN - never queries the remaining record types.
    assert calls["n"] == 1


async def test_dns_lookup_no_mx_records_is_empty_array_not_an_error(
    web_client, api_key, monkeypatch
):
    async def _fake_query(resolver, domain, rtype):
        if rtype == "MX":
            return [], False  # zero results, not NXDOMAIN
        return [f"{rtype.lower()}-value"], False

    monkeypatch.setattr(web_tools_router, "_query_dns_record", _fake_query)

    resp = await web_client.post(
        "/v1/web_tools/dns_lookup",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["error"] is None
    assert data["records"]["MX"] == []
    assert data["records"]["A"] == ["a-value"]
    assert data["records"]["TXT"] == ["txt-value"]


async def test_dns_lookup_missing_domain_is_400(web_client, api_key):
    resp = await web_client.post(
        "/v1/web_tools/dns_lookup",
        json={"domain": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "DOMAIN_REQUIRED"


async def test_dns_lookup_generic_exception_returns_500_without_leak(
    web_client, api_key, monkeypatch
):
    async def _boom(resolver, domain, rtype):
        raise RuntimeError("SECRET_MARKER_dns db_password=hunter2 at web_tools.py")

    monkeypatch.setattr(web_tools_router, "_query_dns_record", _boom)

    resp = await web_client.post(
        "/v1/web_tools/dns_lookup",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "DNS_LOOKUP_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "RuntimeError" not in resp.text


@pytest.mark.parametrize("target", SSRF_TARGETS)
async def test_dns_lookup_blocks_ssrf_targets(web_client, api_key, target):
    domain = f"[{target}]" if ":" in target else target
    resp = await web_client.post(
        "/v1/web_tools/dns_lookup",
        json={"domain": domain},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["error"] == "Cannot check internal or reserved network addresses"
    assert data["records"] == {t: [] for t in web_tools_router.DNS_RECORD_TYPES}


# ===========================================================================
# /web_tools/ssl_checker
# ===========================================================================

async def test_ssl_checker_happy_path_valid_cert(
    web_client, api_key, monkeypatch, valid_cert_der
):
    def _fake_fetch(hostname, port=443, timeout=5.0):
        return {
            "der": valid_cert_der, "verified": True,
            "verify_error": None, "connect_error": None,
        }

    monkeypatch.setattr(web_tools_router, "_fetch_certificate_der", _fake_fetch)

    resp = await web_client.post(
        "/v1/web_tools/ssl_checker",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["domain"] == "example.com"
    assert data["valid"] is True
    assert data["issuer"] == {"commonName": "example.com"}
    assert data["subject"] == {"commonName": "example.com"}
    assert data["valid_from"] and data["valid_until"]
    assert isinstance(data["days_until_expiry"], int)
    assert data["is_self_signed"] is True  # issuer == subject by construction here
    assert data["error"] is None


async def test_ssl_checker_expired_cert(web_client, api_key, monkeypatch, expired_cert_der):
    def _fake_fetch(hostname, port=443, timeout=5.0):
        return {
            "der": expired_cert_der, "verified": False,
            "verify_error": "expired", "connect_error": None,
        }

    monkeypatch.setattr(web_tools_router, "_fetch_certificate_der", _fake_fetch)

    resp = await web_client.post(
        "/v1/web_tools/ssl_checker",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["valid"] is False
    assert data["issuer"] is not None
    assert data["subject"] is not None
    assert data["valid_from"] and data["valid_until"]
    assert data["days_until_expiry"] < 0
    assert data["error"] == "Certificate has expired"


async def test_ssl_checker_self_signed_cert(web_client, api_key, monkeypatch, self_signed_cert_der):
    def _fake_fetch(hostname, port=443, timeout=5.0):
        return {
            "der": self_signed_cert_der, "verified": False,
            "verify_error": "self_signed", "connect_error": None,
        }

    monkeypatch.setattr(web_tools_router, "_fetch_certificate_der", _fake_fetch)

    resp = await web_client.post(
        "/v1/web_tools/ssl_checker",
        json={"domain": "selfsigned.example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["valid"] is False
    assert data["is_self_signed"] is True
    assert data["issuer"] is not None
    assert data["subject"] is not None
    assert data["error"] == "Certificate is self-signed"


async def test_ssl_checker_hostname_mismatch_cert(
    web_client, api_key, monkeypatch, mismatch_cert_der
):
    def _fake_fetch(hostname, port=443, timeout=5.0):
        return {
            "der": mismatch_cert_der, "verified": False,
            "verify_error": "hostname_mismatch", "connect_error": None,
        }

    monkeypatch.setattr(web_tools_router, "_fetch_certificate_der", _fake_fetch)

    resp = await web_client.post(
        "/v1/web_tools/ssl_checker",
        json={"domain": "requested-domain.example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["valid"] is False
    assert data["issuer"] is not None
    assert data["subject"] is not None
    assert data["error"] == "Certificate hostname does not match the domain"


@pytest.mark.parametrize(
    "connect_error,expected_message",
    [
        ("timeout", "Connection to the domain timed out"),
        ("refused", "Connection to the domain was refused"),
        ("dns", "Could not resolve the domain"),
        ("connection_failed", "Unable to establish a TLS connection on port 443"),
    ],
)
async def test_ssl_checker_connect_errors(
    web_client, api_key, monkeypatch, connect_error, expected_message
):
    def _fake_fetch(hostname, port=443, timeout=5.0):
        return {
            "der": None, "verified": False,
            "verify_error": None, "connect_error": connect_error,
        }

    monkeypatch.setattr(web_tools_router, "_fetch_certificate_der", _fake_fetch)

    resp = await web_client.post(
        "/v1/web_tools/ssl_checker",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["valid"] is False
    assert data["issuer"] is None
    assert data["subject"] is None
    assert data["error"] == expected_message


async def test_ssl_checker_missing_domain_is_400(web_client, api_key):
    resp = await web_client.post(
        "/v1/web_tools/ssl_checker",
        json={"domain": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "DOMAIN_REQUIRED"


async def test_ssl_checker_generic_fetch_exception_returns_500_without_leak(
    web_client, api_key, monkeypatch
):
    def _boom(hostname, port=443, timeout=5.0):
        raise RuntimeError("SECRET_MARKER_ssl db_password=hunter2 at web_tools.py")

    monkeypatch.setattr(web_tools_router, "_fetch_certificate_der", _boom)

    resp = await web_client.post(
        "/v1/web_tools/ssl_checker",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "SSL_CHECK_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "RuntimeError" not in resp.text


async def test_ssl_checker_unparseable_certificate_returns_500_without_leak(
    web_client, api_key, monkeypatch
):
    """Real (unmocked) `x509.load_der_x509_certificate` failure path - the
    DER bytes are deliberately garbage, so the actual cryptography-library
    exception, not a monkeypatched one, drives this branch."""
    def _fake_fetch(hostname, port=443, timeout=5.0):
        return {
            "der": b"not-real-der-bytes", "verified": True,
            "verify_error": None, "connect_error": None,
        }

    monkeypatch.setattr(web_tools_router, "_fetch_certificate_der", _fake_fetch)

    resp = await web_client.post(
        "/v1/web_tools/ssl_checker",
        json={"domain": "example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "SSL_CHECK_FAILED"
    assert "der bytes" not in resp.text


@pytest.mark.parametrize("target", SSRF_TARGETS)
async def test_ssl_checker_blocks_ssrf_targets(web_client, api_key, target):
    domain = f"[{target}]" if ":" in target else target
    resp = await web_client.post(
        "/v1/web_tools/ssl_checker",
        json={"domain": domain},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()["data"]
    assert data["valid"] is False
    assert data["issuer"] is None
    assert data["subject"] is None
    assert data["error"] == "Cannot check internal or reserved network addresses"


# ===========================================================================
# check_url() redirect-target SSRF guard
#
# Regression coverage for the redirect-based SSRF bypass: `check_url()`
# previously validated only the *original* hostname, then let aiohttp
# (`allow_redirects=True`) follow the entire redirect chain with zero
# re-validation of any `Location` target. These tests drive `check_url()`
# directly against a scripted fake `aiohttp.ClientSession` (no real network
# I/O - same idiom the module docstring above uses for `assert_host_is_safe`:
# IP-literal hosts resolve to themselves via `socket.getaddrinfo()` with no
# DNS lookup, so `1.1.1.1`/`9.9.9.9`/`127.0.0.1` are all safe to use as
# "real" (unmocked) SSRF-guard exercises here too), rather than mocking
# `check_url` itself away as the endpoint-level tests above do.
# ===========================================================================

class _FakeResponse:
    """Mimics just enough of `aiohttp.ClientResponse` for `check_url()`'s
    manual redirect loop: `.status`, `.headers.get("Location")`, and the
    attributes `aiohttp.ClientResponseError`/`TooManyRedirects` read off of
    it (`request_info`, `history`), plus async-context-manager support to
    match `async with session.get(...) as response:`."""

    def __init__(self, status: int, location: str | None = None):
        self.status = status
        self.headers = {"Location": location} if location else {}
        self.request_info = aiohttp.RequestInfo(
            url="http://example.invalid", method="GET", headers={}, real_url="http://example.invalid",
        )
        self.history = ()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _ScriptedRedirectSession:
    """Fake `aiohttp.ClientSession` that serves one scripted `_FakeResponse`
    per URL requested, in order, and records every URL `check_url()` asked
    for - so tests can assert exactly which hops were (or weren't) followed.
    """

    def __init__(self, responses_by_url: dict[str, "_FakeResponse | list[_FakeResponse]"]):
        self._responses = responses_by_url
        self.requested_urls: list[str] = []

    def get(self, url, allow_redirects=False, timeout=5):
        assert allow_redirects is False, (
            "check_url() must disable aiohttp's own redirect-following and walk "
            "the chain manually so each hop can be SSRF-checked"
        )
        self.requested_urls.append(url)
        entry = self._responses[url]
        if isinstance(entry, list):
            return entry.pop(0)
        return entry


async def test_check_url_rejects_redirect_to_private_host():
    """The bypass this fixes: a safe, public first hop 302s straight to a
    loopback address. Must be rejected before that hop is ever followed."""
    session = _ScriptedRedirectSession({
        "http://1.1.1.1/": _FakeResponse(302, location="http://127.0.0.1/secret"),
    })

    with pytest.raises(web_tools_router.UnsafeHostError):
        await web_tools_router.check_url(session, "http://1.1.1.1/")

    # Only the first (safe) hop was actually requested - the unsafe target
    # was never connected to.
    assert session.requested_urls == ["http://1.1.1.1/"]


async def test_check_url_rejects_redirect_to_metadata_endpoint():
    """Same bypass, cloud-metadata-endpoint flavor (169.254.169.254) -
    the concrete example from the finding."""
    session = _ScriptedRedirectSession({
        "http://1.1.1.1/": _FakeResponse(
            302, location="http://169.254.169.254/latest/meta-data/"
        ),
    })

    with pytest.raises(web_tools_router.UnsafeHostError):
        await web_tools_router.check_url(session, "http://1.1.1.1/")

    assert session.requested_urls == ["http://1.1.1.1/"]


async def test_check_url_rejects_unsafe_host_on_a_later_hop():
    """The unsafe redirect doesn't have to be the first hop - a chain that
    starts and stays safe for a couple of hops before turning unsafe must
    still be caught."""
    session = _ScriptedRedirectSession({
        "http://1.1.1.1/": _FakeResponse(302, location="http://9.9.9.9/step2"),
        "http://9.9.9.9/step2": _FakeResponse(302, location="http://10.0.0.5/internal"),
    })

    with pytest.raises(web_tools_router.UnsafeHostError):
        await web_tools_router.check_url(session, "http://1.1.1.1/")

    assert session.requested_urls == ["http://1.1.1.1/", "http://9.9.9.9/step2"]


async def test_check_url_follows_redirect_chain_of_safe_hosts():
    """Happy path must be unchanged: a redirect chain that stays on safe,
    public hosts throughout still resolves to the final status."""
    session = _ScriptedRedirectSession({
        "http://1.1.1.1/": _FakeResponse(301, location="http://9.9.9.9/final"),
        "http://9.9.9.9/final": _FakeResponse(200),
    })

    is_up, status = await web_tools_router.check_url(session, "http://1.1.1.1/")

    assert is_up is True
    assert status == 200
    assert session.requested_urls == ["http://1.1.1.1/", "http://9.9.9.9/final"]


async def test_check_url_no_redirect_still_works():
    """A plain, non-redirecting request behaves exactly as before."""
    session = _ScriptedRedirectSession({"http://1.1.1.1/": _FakeResponse(200)})

    is_up, status = await web_tools_router.check_url(session, "http://1.1.1.1/")

    assert is_up is True
    assert status == 200
    assert session.requested_urls == ["http://1.1.1.1/"]


async def test_check_url_relative_redirect_location_resolves_against_current_url():
    """`Location` headers are frequently relative (path-only or protocol-
    relative), not absolute - `urljoin()` must resolve them against the hop
    they came from, and the resolved host is what gets SSRF-checked."""
    session = _ScriptedRedirectSession({
        "http://1.1.1.1/start": _FakeResponse(302, location="/next"),
        "http://1.1.1.1/next": _FakeResponse(200),
    })

    is_up, status = await web_tools_router.check_url(session, "http://1.1.1.1/start")

    assert is_up is True
    assert status == 200
    assert session.requested_urls == ["http://1.1.1.1/start", "http://1.1.1.1/next"]


async def test_check_url_too_many_redirects_raises_cleanly(monkeypatch):
    """A redirect loop degrades to a clean, existing-exception-shaped
    failure - not an unbounded loop or an unhandled crash.

    `check_url()` is wrapped in a `@retry(... retry_if_exception_type
    (aiohttp.ClientResponseError))` decorator, and `aiohttp.TooManyRedirects`
    is itself a `ClientResponseError` subclass (matches real aiohttp), so it
    is retried the same way an exhausted-retries 429 already is. Pinning
    `stop` down to a single attempt here avoids paying the wall-clock cost of
    3 real exponential-backoff sleeps for what this test needs to assert.

    Note (pre-existing, out of scope for this fix): tenacity's `@retry`
    without `reraise=True` wraps the final exhausted-attempt exception in
    `tenacity.RetryError` rather than re-raising it directly - confirmed this
    already applies identically to the pre-existing 429-exhausted-retries
    path, not something newly introduced by the redirect-hop handling here.
    Both `validate_url` and `website_down_detector` already have a generic
    `except Exception` fallback that turns this into a clean 500 rather than
    a crash, so callers are not left unhandled either way - just not the
    nicer `ClientResponseError`-shaped message a caller might expect. Not
    fixing that pre-existing gap here since it's unrelated to the SSRF
    findings this change addresses.
    """
    monkeypatch.setattr(web_tools_router.check_url.retry, "stop", tenacity.stop_after_attempt(1))
    session = _ScriptedRedirectSession({
        "http://1.1.1.1/": [_FakeResponse(302, location="http://1.1.1.1/") for _ in range(10)],
    })

    with pytest.raises(tenacity.RetryError) as exc_info:
        await web_tools_router.check_url(session, "http://1.1.1.1/")
    assert isinstance(exc_info.value.last_attempt.exception(), aiohttp.TooManyRedirects)

    # Bounded: at most _MAX_REDIRECT_HOPS + 1 requests, not attempted forever.
    assert len(session.requested_urls) == web_tools_router._MAX_REDIRECT_HOPS + 1


async def test_website_down_detector_redirect_hop_ssrf_is_blocked_cleanly(
    web_client, api_key, monkeypatch
):
    """End-to-end shape check at the endpoint level: when `check_url()`
    raises `UnsafeHostError` because a redirect hop targeted an unsafe host,
    `website_down_detector` must degrade to the same clean SSRF-blocked
    result as its own pre-request guard, not a 500 or an unhandled error."""

    async def _fake_check_url(session, url):
        raise web_tools_router.UnsafeHostError("blocked redirect hop")

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await web_client.post(
        "/v1/web_tools/website_down_detector",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    data = body["data"]
    assert data["is_up"] is False
    assert data["error"] == web_tools_router._SSRF_BLOCKED_MESSAGE


# ===========================================================================
# Credential redaction in logs (Finding 3 - website_down_detector/check_url
# must never log embedded URL userinfo verbatim)
# ===========================================================================

async def test_redact_url_credentials_strips_userinfo():
    redacted = web_tools_router._redact_url_credentials(
        "https://admin:hunter2@internal.example.com/path?x=1"
    )
    assert "admin" not in redacted
    assert "hunter2" not in redacted
    assert redacted == "https://internal.example.com/path?x=1"


async def test_redact_url_credentials_preserves_port_without_userinfo():
    redacted = web_tools_router._redact_url_credentials(
        "https://user:pw@example.com:8443/path"
    )
    assert "user" not in redacted
    assert "pw" not in redacted
    assert redacted == "https://example.com:8443/path"


async def test_redact_url_credentials_noop_when_no_credentials():
    url = "https://example.com/path?x=1"
    assert web_tools_router._redact_url_credentials(url) == url


async def test_website_down_detector_does_not_log_embedded_credentials(
    web_client, api_key, monkeypatch, caplog
):
    async def _fake_check_url(session, url):
        return True, 200

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    with caplog.at_level("DEBUG", logger=web_tools_router.logger.name):
        resp = await web_client.post(
            "/v1/web_tools/website_down_detector",
            json={"url": "https://admin:hunter2@example.com/"},
            headers={"X-API-Key": api_key["key"]},
        )

    assert resp.status_code == 200, resp.text
    log_text = "\n".join(record.getMessage() for record in caplog.records)
    assert "hunter2" not in log_text
    # The response body still echoes the caller's own submitted URL back to
    # them (not a log, and it's their own credential) - only logs are redacted.
    assert resp.json()["data"]["url"] == "https://admin:hunter2@example.com/"
