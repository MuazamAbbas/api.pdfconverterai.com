"""Unit coverage for `app/services/seo/seo_audit.py` (Handbook Part D.1),
the service backing `POST /v1/seo_tools/seo_audit` (feature-spec approved
2026-08-31, docs/roadmap/SPRINT_STATUS.md "feature-spec approved: SEO Audit
(last v1-parity tool)" entry).

No real network I/O anywhere in this file: `check_url()` and
`aiohttp.ClientSession` are monkeypatched at the `app.services.seo.seo_audit`
module's own call sites, mirroring the mocking convention already
established in `tests/test_web_tools_uptime_dns_ssl.py`/
`tests/test_web_tools_whois_ip_speed.py`.
"""
import asyncio

import aiohttp
import pytest
from bs4 import BeautifulSoup

import app.services.seo.seo_audit as seo_audit
from app.shared.network_security import UnsafeHostError

pytestmark = pytest.mark.asyncio(loop_scope="session")


# ===========================================================================
# Meta tags
# ===========================================================================

async def test_analyze_meta_tags_good_title_and_description():
    html = (
        "<html><head>"
        "<title>A well sized SEO title for testing</title>"
        '<meta name="description" content="'
        + ("x" * 100) + '">'
        "</head></html>"
    )
    soup = BeautifulSoup(html, "html.parser")
    result = seo_audit._analyze_meta_tags(soup)
    checks = {f["check"]: f for f in result["findings"]}
    assert checks["title_length"]["passed"] is True
    assert checks["description_length"]["passed"] is True


async def test_analyze_meta_tags_missing_title_and_description():
    soup = BeautifulSoup("<html><head></head></html>", "html.parser")
    result = seo_audit._analyze_meta_tags(soup)
    checks = {f["check"]: f for f in result["findings"]}
    assert checks["title_present"]["passed"] is False
    assert checks["description_present"]["passed"] is False


async def test_analyze_meta_tags_title_too_short_fails():
    soup = BeautifulSoup("<html><head><title>Hi</title></head></html>", "html.parser")
    result = seo_audit._analyze_meta_tags(soup)
    checks = {f["check"]: f for f in result["findings"]}
    assert checks["title_length"]["passed"] is False
    assert checks["title_length"]["suggestion"] is not None


# ===========================================================================
# Heading structure
# ===========================================================================

async def test_analyze_headings_no_headings_found():
    soup = BeautifulSoup("<html><body><p>no headings here</p></body></html>", "html.parser")
    result = seo_audit._analyze_headings(soup)
    assert result["findings"][0]["check"] == "heading_present"
    assert result["findings"][0]["passed"] is False


async def test_analyze_headings_single_h1_in_order_passes():
    soup = BeautifulSoup(
        "<html><body><h1>Title</h1><h2>Section</h2><h3>Sub</h3></body></html>", "html.parser"
    )
    result = seo_audit._analyze_headings(soup)
    checks = {f["check"]: f for f in result["findings"]}
    assert checks["h1_present"]["passed"] is True
    assert "single_h1" not in checks
    assert checks["heading_order"]["passed"] is True


async def test_analyze_headings_multiple_h1_fails():
    soup = BeautifulSoup("<html><body><h1>One</h1><h1>Two</h1></body></html>", "html.parser")
    result = seo_audit._analyze_headings(soup)
    checks = {f["check"]: f for f in result["findings"]}
    assert checks["single_h1"]["passed"] is False


async def test_analyze_headings_skipped_level_fails():
    soup = BeautifulSoup("<html><body><h1>Title</h1><h4>Skipped</h4></body></html>", "html.parser")
    result = seo_audit._analyze_headings(soup)
    checks = {f["check"]: f for f in result["findings"]}
    assert checks["heading_order"]["passed"] is False


# ===========================================================================
# Image alt-text coverage
# ===========================================================================

async def test_analyze_images_no_images_passes_trivially():
    soup = BeautifulSoup("<html><body><p>no images</p></body></html>", "html.parser")
    result = seo_audit._analyze_images(soup)
    assert result["total_images"] == 0
    assert result["coverage_percent"] == 100.0
    assert result["findings"][0]["passed"] is True


async def test_analyze_images_full_alt_coverage_passes():
    soup = BeautifulSoup(
        '<html><body><img src="a.png" alt="A cat"><img src="b.png" alt="A dog"></body></html>',
        "html.parser",
    )
    result = seo_audit._analyze_images(soup)
    assert result["coverage_percent"] == 100.0
    assert result["findings"][0]["passed"] is True


async def test_analyze_images_low_alt_coverage_fails():
    soup = BeautifulSoup(
        '<html><body><img src="a.png"><img src="b.png"><img src="c.png" alt="ok"></body></html>',
        "html.parser",
    )
    result = seo_audit._analyze_images(soup)
    assert result["coverage_percent"] < seo_audit._IMAGE_ALT_COVERAGE_PASS_THRESHOLD
    assert result["findings"][0]["passed"] is False


async def test_analyze_images_whitespace_only_alt_does_not_count_as_meaningful():
    soup = BeautifulSoup('<html><body><img src="a.png" alt="   "></body></html>', "html.parser")
    result = seo_audit._analyze_images(soup)
    assert result["coverage_percent"] == 0.0


# ===========================================================================
# Link extraction
# ===========================================================================

async def test_extract_links_filters_and_dedupes():
    html = """
    <html><body>
    <a href="https://example.com/a">A</a>
    <a href="/b">B</a>
    <a href="#section">anchor only</a>
    <a href="mailto:test@example.com">mail</a>
    <a href="javascript:void(0)">js</a>
    <a href="https://example.com/a#frag">dup of A with fragment</a>
    </body></html>
    """
    soup = BeautifulSoup(html, "html.parser")
    links = seo_audit._extract_links(soup, "https://example.com/")
    assert links == ["https://example.com/a", "https://example.com/b"]


async def test_extract_links_respects_limit():
    html = "<html><body>" + "".join(
        f'<a href="https://example.com/{i}">{i}</a>' for i in range(30)
    ) + "</body></html>"
    soup = BeautifulSoup(html, "html.parser")
    links = seo_audit._extract_links(soup, "https://example.com/", limit=20)
    assert len(links) == 20


# ===========================================================================
# Broken links (bounded concurrency + global wall-clock budget)
# ===========================================================================

async def test_check_broken_links_no_links_found():
    result = await seo_audit._check_broken_links(session=object(), links=[])
    assert result["checked"] == 0
    assert result["findings"][0]["passed"] is True


async def test_check_broken_links_all_reachable(monkeypatch):
    async def _fake_check_url(session, url):
        return True, 200

    monkeypatch.setattr(seo_audit, "check_url", _fake_check_url)
    links = ["https://example.com/1", "https://example.com/2"]
    result = await seo_audit._check_broken_links(session=object(), links=links)
    assert result["checked"] == 2
    assert result["findings"][0]["passed"] is True


async def test_check_broken_links_reports_broken_ones(monkeypatch):
    async def _fake_check_url(session, url):
        if url.endswith("/broken"):
            return False, 404
        return True, 200

    monkeypatch.setattr(seo_audit, "check_url", _fake_check_url)
    links = ["https://example.com/ok", "https://example.com/broken"]
    result = await seo_audit._check_broken_links(session=object(), links=links)
    assert result["checked"] == 2
    assert result["findings"][0]["passed"] is False
    assert "1 of 2" in result["findings"][0]["message"]


async def test_check_broken_links_ssrf_blocked_link_counts_as_broken(monkeypatch):
    async def _fake_check_url(session, url):
        raise UnsafeHostError("blocked")

    monkeypatch.setattr(seo_audit, "check_url", _fake_check_url)
    result = await seo_audit._check_broken_links(session=object(), links=["http://169.254.169.254/"])
    assert result["details"][0]["error"] == "internal_or_reserved_address"
    assert result["details"][0]["ok"] is False


async def test_check_broken_links_enforces_global_wall_clock_budget(monkeypatch):
    """Bounds the WHOLE phase, not per link: a straggler that would run far
    longer than the budget must be cut off, with the fast links' results
    still returned - not blown out into a multi-minute stall."""
    monkeypatch.setattr(seo_audit, "_BROKEN_LINK_BUDGET_SECONDS", 0.2)

    async def _fake_check_url(session, url):
        if url.endswith("/slow"):
            await asyncio.sleep(5)  # far longer than the 0.2s test budget
            return True, 200
        return True, 200

    monkeypatch.setattr(seo_audit, "check_url", _fake_check_url)
    links = ["https://example.com/fast", "https://example.com/slow"]

    start = asyncio.get_event_loop().time()
    result = await seo_audit._check_broken_links(session=object(), links=links)
    elapsed = asyncio.get_event_loop().time() - start

    assert elapsed < 2, "must not block anywhere near the slow link's own 5s sleep"
    assert result["checked"] == 1
    assert "skipped" in result["findings"][0]["message"]


# ===========================================================================
# Sitemap.xml presence
# ===========================================================================

async def test_check_sitemap_found(monkeypatch):
    async def _fake_check_url(session, url):
        assert url == "https://example.com/sitemap.xml"
        return True, 200

    monkeypatch.setattr(seo_audit, "check_url", _fake_check_url)
    result = await seo_audit._check_sitemap(session=object(), origin="https://example.com")
    assert result["findings"][0]["passed"] is True


async def test_check_sitemap_not_found(monkeypatch):
    async def _fake_check_url(session, url):
        return False, 404

    monkeypatch.setattr(seo_audit, "check_url", _fake_check_url)
    result = await seo_audit._check_sitemap(session=object(), origin="https://example.com")
    assert result["findings"][0]["passed"] is False
    assert result["findings"][0]["suggestion"] is not None


async def test_check_sitemap_ssrf_blocked(monkeypatch):
    async def _fake_check_url(session, url):
        raise UnsafeHostError("blocked")

    monkeypatch.setattr(seo_audit, "check_url", _fake_check_url)
    result = await seo_audit._check_sitemap(session=object(), origin="http://169.254.169.254")
    assert result["findings"][0]["passed"] is False
    assert result["findings"][0]["message"] == seo_audit._SSRF_BLOCKED_MESSAGE


async def test_check_sitemap_timeout_degrades_cleanly(monkeypatch):
    async def _fake_check_url(session, url):
        raise asyncio.TimeoutError()

    monkeypatch.setattr(seo_audit, "check_url", _fake_check_url)
    result = await seo_audit._check_sitemap(session=object(), origin="https://example.com")
    assert result["findings"][0]["passed"] is False


# ===========================================================================
# Orchestration - SSRF blocked at the main-host level
# ===========================================================================

async def test_run_seo_audit_blocks_ssrf_targets_before_any_fetch(monkeypatch):
    fetch_called = False

    async def _fail_if_called(url):
        nonlocal fetch_called
        fetch_called = True
        raise AssertionError("must not fetch a host that failed the SSRF guard")

    monkeypatch.setattr(seo_audit, "_fetch_main_page", _fail_if_called)

    result = await seo_audit.run_seo_audit("http://169.254.169.254/")

    assert fetch_called is False
    assert result["reachable"] is False
    assert result["error"] == seo_audit._SSRF_BLOCKED_MESSAGE
    for key in (
        "meta_tags", "heading_structure", "broken_links", "sitemap", "image_alt_text", "page_speed",
    ):
        assert key in result


async def test_run_seo_audit_rejects_empty_url():
    with pytest.raises(ValueError):
        await seo_audit.run_seo_audit("")


async def test_run_seo_audit_rejects_unparseable_url():
    with pytest.raises(ValueError):
        await seo_audit.run_seo_audit("http://")


# ===========================================================================
# Main page fetch - redirect hop SSRF re-validation
# ===========================================================================

class _FakeMainPageResponse:
    def __init__(self, status: int, location: str | None = None, body: bytes = b""):
        self.status = status
        self.headers = {"Location": location} if location else {}
        self._body = body
        self.content = self

    async def read(self, n: int) -> bytes:
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False


class _FakeMainPageSession:
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


def _patch_main_page_session(monkeypatch, session):
    def _factory(*args, **kwargs):
        return session

    monkeypatch.setattr(seo_audit.aiohttp, "ClientSession", _factory)


async def test_fetch_main_page_happy_path(monkeypatch):
    session = _FakeMainPageSession({
        "https://example.com/": _FakeMainPageResponse(200, body=b"<html></html>"),
    })
    _patch_main_page_session(monkeypatch, session)

    result = await seo_audit._fetch_main_page("https://example.com/")

    assert result["reachable"] is True
    assert result["status_code"] == 200
    assert result["html"] == "<html></html>"
    assert result["error"] is None


async def test_fetch_main_page_rejects_redirect_to_unsafe_host(monkeypatch):
    session = _FakeMainPageSession({
        "https://example.com/": _FakeMainPageResponse(302, location="http://127.0.0.1/secret"),
    })
    _patch_main_page_session(monkeypatch, session)

    result = await seo_audit._fetch_main_page("https://example.com/")

    assert result["reachable"] is False
    assert result["error"] == seo_audit._SSRF_BLOCKED_MESSAGE
    # The unsafe redirect target was never actually requested.
    assert session.requested_urls == ["https://example.com/"]


async def test_fetch_main_page_non_2xx_status_still_reachable_with_error_noted(monkeypatch):
    """A 404/500 page still has HTML worth auditing (meta/heading/image
    checks should still run against whatever the server returned) - only
    genuine network-level failures should mark the page unreachable."""
    session = _FakeMainPageSession({
        "https://example.com/": _FakeMainPageResponse(404, body=b"<html><title>Not Found</title></html>"),
    })
    _patch_main_page_session(monkeypatch, session)

    result = await seo_audit._fetch_main_page("https://example.com/")

    assert result["reachable"] is True
    assert result["status_code"] == 404
    assert result["error"] == "Page returned status 404"
    assert result["html"] is not None


async def test_fetch_main_page_connector_error_degrades_cleanly(monkeypatch):
    def _raise(*args, **kwargs):
        raise aiohttp.ClientConnectorError(
            connection_key=None, os_error=OSError("boom")
        )

    monkeypatch.setattr(seo_audit.aiohttp, "ClientSession", _raise)

    result = await seo_audit._fetch_main_page("https://example.com/")
    assert result["reachable"] is False
    assert result["html"] is None


# ===========================================================================
# Page speed
# ===========================================================================

async def test_analyze_page_speed_unreachable_page():
    page = {"reachable": False, "error": "Website is unreachable"}
    result = seo_audit._analyze_page_speed(page)
    assert result["metrics"] is None
    assert result["findings"][0]["passed"] is False


async def test_analyze_page_speed_fast_page_passes():
    page = {
        "reachable": True, "error": None,
        "total_time_ms": 500, "ttfb_ms": 100,
        "dns_time_ms": 10, "connect_time_ms": 20,
        "status_code": 200, "content_size_bytes": 1000,
    }
    result = seo_audit._analyze_page_speed(page)
    checks = {f["check"]: f for f in result["findings"]}
    assert checks["total_load_time"]["passed"] is True
    assert checks["time_to_first_byte"]["passed"] is True
    assert result["metrics"]["status_code"] == 200


async def test_analyze_page_speed_slow_page_fails():
    page = {
        "reachable": True, "error": None,
        "total_time_ms": 9000, "ttfb_ms": 4000,
        "dns_time_ms": 10, "connect_time_ms": 20,
        "status_code": 200, "content_size_bytes": 1000,
    }
    result = seo_audit._analyze_page_speed(page)
    checks = {f["check"]: f for f in result["findings"]}
    assert checks["total_load_time"]["passed"] is False
    assert checks["time_to_first_byte"]["passed"] is False
