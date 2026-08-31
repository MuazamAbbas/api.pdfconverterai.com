"""SEO Audit (v1-parity tool, feature-spec approved 2026-08-31 -
docs/roadmap/SPRINT_STATUS.md "feature-spec approved: SEO Audit (last
v1-parity tool)" entry).

Audits a URL across 6 areas - meta tags, heading structure, broken links,
sitemap.xml presence, image alt-text coverage, and page speed - matching
v1's `pdfconverterai.com/tools/seo/seo-audit` framing (one block per
category, pass/fail findings + actionable suggestions).

Tier 1 (Handbook Part I.2), conditional on the bounded, wall-clock-budgeted
broken-link phase below (extends the ADR-017/`validate_url` bounded-
external-call precedent - no new ADR needed per the approved feature-spec).

SSRF hardening (Handbook Part C.10): every host this module touches (the
main URL, every redirect hop of the main fetch, `sitemap.xml`, and every
checked link) is guarded by `app.shared.network_security.assert_host_is_safe`
- reused, not reimplemented, via the relocated
`app.shared.web.redirect_fetch.check_url` for links/sitemap, and via
explicit per-hop calls in `_fetch_main_page` (mirroring
`app/routers/web_tools.py::speed_test`'s own manual redirect loop, which
needs the same per-hop re-validation because it also captures per-hop
timing and therefore can't just delegate to `check_url`). Per-hop timing
itself reuses the relocated `app.shared.web.speed_trace._build_speed_trace_config`
- also moved out of `app/routers/web_tools.py` in this change, so this
service never imports from a router module (Handbook Part C.3).

No unbounded crawling: link discovery is capped at `_MAX_LINKS_CHECKED`,
the main page body is capped at `_MAX_HTML_BYTES`, and broken-link checking
runs under bounded concurrency plus a global wall-clock budget
(`_BROKEN_LINK_BUDGET_SECONDS`, matching the ADR-017 `speed_test` ~30-45s
ceiling) enforced across the whole phase, not per link.
"""
import asyncio
import logging
import re
import time
import urllib.parse

import aiohttp
from bs4 import BeautifulSoup

from app.shared.network_security import UnsafeHostError, assert_host_is_safe
from app.shared.web.redirect_fetch import (
    _MAX_REDIRECT_HOPS,
    _REDIRECT_STATUSES,
    _redact_url_credentials,
    check_url,
)
from app.shared.web.speed_trace import _build_speed_trace_config

logger = logging.getLogger(__name__)

_SSRF_BLOCKED_MESSAGE = "Cannot audit internal or reserved network addresses"

_TITLE_MIN_LEN = 10
_TITLE_MAX_LEN = 60
_DESCRIPTION_MIN_LEN = 50
_DESCRIPTION_MAX_LEN = 160

_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

_IMAGE_ALT_COVERAGE_PASS_THRESHOLD = 80.0

_MAX_LINKS_CHECKED = 20
_BROKEN_LINK_CONCURRENCY = 5
# Matches the ADR-017 `speed_test` bounded-external-call ceiling
# (~30s worst case, 5s x up to 6 hops) - picked at the upper end of the
# feature-spec's 30-45s range so genuinely concurrent, SSRF-checked link
# checks (up to `_MAX_LINKS_CHECKED` links, `_BROKEN_LINK_CONCURRENCY` at a
# time) have realistic headroom without ever letting the phase run long
# enough to threaten this Tier-1 endpoint's synchronous request/response
# budget. Enforced across the whole phase (`asyncio.wait(..., timeout=...)`
# below), not stacked per link - a slow/rate-limited link degrades to
# "skipped, time budget exceeded" instead of blocking the others.
_BROKEN_LINK_BUDGET_SECONDS = 40

_MAIN_PAGE_TIMEOUT = aiohttp.ClientTimeout(total=10)
# Caps how much of the main page body is read into memory for parsing -
# plenty for meta/heading/image/link analysis, and prevents an
# unexpectedly huge remote response from being an unbounded-memory/-time
# sink for what is otherwise a small, bounded Tier-1 call.
_MAX_HTML_BYTES = 2_000_000

_LINK_CHECK_TIMEOUT = aiohttp.ClientTimeout(total=10)


def _finding(check: str, passed: bool, message: str, suggestion: str | None) -> dict:
    return {"check": check, "passed": passed, "message": message, "suggestion": suggestion}


def _unreachable_block(message: str) -> dict:
    return {
        "findings": [
            _finding(
                "page_fetch", False, message,
                "Ensure the URL is publicly reachable, then re-run the audit.",
            )
        ]
    }


def _blocked_block() -> dict:
    return {
        "findings": [
            _finding("host_safety", False, _SSRF_BLOCKED_MESSAGE, "Provide a public, externally reachable URL.")
        ]
    }


def _blocked_report(raw_url: str) -> dict:
    """Returned when the main URL's hostname itself fails
    `assert_host_is_safe` - short-circuits before any fetch is attempted
    (sitemap/links would resolve to the same disallowed host anyway)."""
    block = _blocked_block()
    return {
        "url": raw_url,
        "final_url": raw_url,
        "reachable": False,
        "error": _SSRF_BLOCKED_MESSAGE,
        "meta_tags": block,
        "heading_structure": block,
        "broken_links": {**block, "checked": 0, "total_links_found": 0, "details": []},
        "sitemap": block,
        "image_alt_text": block,
        "page_speed": {"findings": block["findings"], "metrics": None},
    }


# ---------------------------------------------------------------------------
# Main page fetch (SSRF-guarded, bounded redirect hops, timed) - a manual
# redirect loop rather than `check_url()` because this also needs the
# response body (for meta/heading/image/link parsing) and per-hop timing,
# same reasoning `web_tools.py::speed_test` already documents for its own
# equivalent loop.
# ---------------------------------------------------------------------------

def _unreachable_page(url: str, error: str) -> dict:
    return {
        "reachable": False,
        "final_url": url,
        "status_code": None,
        "html": None,
        "content_size_bytes": None,
        "dns_time_ms": None,
        "connect_time_ms": None,
        "ttfb_ms": None,
        "total_time_ms": None,
        "error": error,
    }


async def _fetch_main_page(url: str) -> dict:
    timings: dict = {}
    trace_config = _build_speed_trace_config(timings)
    total_start = time.monotonic()
    current_url = url

    try:
        async with aiohttp.ClientSession(
            trace_configs=[trace_config], timeout=_MAIN_PAGE_TIMEOUT
        ) as session:
            for _hop in range(_MAX_REDIRECT_HOPS + 1):
                timings.clear()
                async with session.get(current_url, allow_redirects=False) as response:
                    location = response.headers.get("Location")
                    if response.status in _REDIRECT_STATUSES and location:
                        next_url = urllib.parse.urljoin(current_url, location)
                        next_hostname = urllib.parse.urlparse(next_url).hostname
                        if next_hostname:
                            # Re-validated per hop for the same reason
                            # `check_url()`/`speed_test` already do it - a
                            # public URL redirecting to an internal address
                            # must not bypass the pre-fetch guard.
                            await assert_host_is_safe(next_hostname)
                        logger.debug(
                            "↪️ SEO audit following redirect: %s -> %s",
                            _redact_url_credentials(current_url), _redact_url_credentials(next_url),
                        )
                        current_url = next_url
                        continue

                    raw_body = await response.content.read(_MAX_HTML_BYTES + 1)
                    truncated = len(raw_body) > _MAX_HTML_BYTES
                    body = raw_body[:_MAX_HTML_BYTES]
                    total_ms = round((time.monotonic() - total_start) * 1000, 2)
                    html_text = body.decode("utf-8", errors="replace")
                    error = None
                    if not (200 <= response.status < 400):
                        error = f"Page returned status {response.status}"
                    if truncated:
                        logger.debug(
                            "SEO audit: main page body truncated at %d bytes for %s",
                            _MAX_HTML_BYTES, _redact_url_credentials(current_url),
                        )
                    return {
                        "reachable": True,
                        "final_url": current_url,
                        "status_code": response.status,
                        "html": html_text,
                        "content_size_bytes": len(body),
                        "dns_time_ms": timings.get("dns_time_ms"),
                        "connect_time_ms": timings.get("connect_time_ms"),
                        "ttfb_ms": timings.get("ttfb_ms"),
                        "total_time_ms": total_ms,
                        "error": error,
                    }

            logger.warning("⚠️ Too many redirects during SEO audit fetch: %s", _redact_url_credentials(url))
            return _unreachable_page(url, "Too many redirects")
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt during SEO audit fetch (redirect hop): %s", url)
        return _unreachable_page(url, _SSRF_BLOCKED_MESSAGE)
    except asyncio.TimeoutError:
        return _unreachable_page(url, "Connection to the domain timed out")
    except aiohttp.ClientConnectorDNSError:
        return _unreachable_page(url, "Could not resolve the domain")
    except aiohttp.ClientConnectorError:
        return _unreachable_page(url, "Unable to establish a connection")
    except aiohttp.ClientError:
        return _unreachable_page(url, "Website is unreachable")


# ---------------------------------------------------------------------------
# Meta tags
# ---------------------------------------------------------------------------

def _analyze_meta_tags(soup: BeautifulSoup) -> dict:
    findings = []

    title_tag = soup.title
    title = title_tag.get_text(strip=True) if title_tag else ""
    if not title:
        findings.append(_finding(
            "title_present", False, "No <title> tag found",
            "Add a descriptive <title> tag to the page.",
        ))
    else:
        title_len = len(title)
        ok = _TITLE_MIN_LEN <= title_len <= _TITLE_MAX_LEN
        findings.append(_finding(
            "title_length", ok, f"Title is {title_len} character(s) long",
            None if ok else (
                f"Aim for a title between {_TITLE_MIN_LEN} and {_TITLE_MAX_LEN} "
                "characters for optimal SERP display."
            ),
        ))

    description_tag = soup.find("meta", attrs={"name": "description"})
    description = (description_tag.get("content") or "").strip() if description_tag else ""
    if not description:
        findings.append(_finding(
            "description_present", False, "No meta description found",
            'Add a <meta name="description"> tag summarizing the page.',
        ))
    else:
        desc_len = len(description)
        ok = _DESCRIPTION_MIN_LEN <= desc_len <= _DESCRIPTION_MAX_LEN
        findings.append(_finding(
            "description_length", ok, f"Meta description is {desc_len} character(s) long",
            None if ok else (
                f"Aim for a description between {_DESCRIPTION_MIN_LEN} and "
                f"{_DESCRIPTION_MAX_LEN} characters."
            ),
        ))

    return {"findings": findings}


# ---------------------------------------------------------------------------
# Heading structure
# ---------------------------------------------------------------------------

def _analyze_headings(soup: BeautifulSoup) -> dict:
    headings = soup.find_all(_HEADING_TAGS)
    if not headings:
        return {
            "findings": [
                _finding(
                    "heading_present", False, "No headings (H1-H6) found on the page",
                    "Add a clear heading structure starting with a single H1.",
                )
            ]
        }

    findings = []
    h1_count = sum(1 for tag in headings if tag.name == "h1")
    findings.append(_finding(
        "h1_present", h1_count >= 1, f"Found {h1_count} <h1> tag(s)",
        None if h1_count >= 1 else "Add exactly one <h1> tag describing the page's main topic.",
    ))
    if h1_count > 1:
        findings.append(_finding(
            "single_h1", False, f"Found {h1_count} <h1> tags - expected exactly one",
            "Use a single <h1> per page and demote the others to <h2>/<h3>.",
        ))

    prev_level = None
    skipped = False
    for tag in headings:
        level = int(tag.name[1])
        if prev_level is not None and level > prev_level + 1:
            skipped = True
            break
        prev_level = level
    findings.append(_finding(
        "heading_order", not skipped,
        "Heading levels increase sequentially" if not skipped
        else "Heading levels skip one or more levels (e.g. H2 straight to H4)",
        None if not skipped else "Avoid skipping heading levels - follow H1 -> H2 -> H3 in order.",
    ))

    return {"findings": findings}


# ---------------------------------------------------------------------------
# Image alt-text coverage
# ---------------------------------------------------------------------------

def _analyze_images(soup: BeautifulSoup) -> dict:
    images = soup.find_all("img")
    total = len(images)
    if total == 0:
        return {
            "findings": [_finding("image_alt_text", True, "No images found on the page", None)],
            "coverage_percent": 100.0,
            "total_images": 0,
        }

    with_alt = sum(1 for img in images if (img.get("alt") or "").strip())
    coverage = round((with_alt / total) * 100, 2)
    ok = coverage >= _IMAGE_ALT_COVERAGE_PASS_THRESHOLD
    return {
        "findings": [
            _finding(
                "image_alt_text", ok,
                f"{with_alt} of {total} image(s) ({coverage}%) have meaningful alt text",
                None if ok else (
                    "Add descriptive alt text to images missing it, for accessibility "
                    "and image-search SEO."
                ),
            )
        ],
        "coverage_percent": coverage,
        "total_images": total,
    }


# ---------------------------------------------------------------------------
# Broken links
# ---------------------------------------------------------------------------

def _extract_links(soup: BeautifulSoup, base_url: str, limit: int = _MAX_LINKS_CHECKED) -> list[str]:
    seen: set[str] = set()
    links: list[str] = []
    for tag in soup.find_all("a", href=True):
        href = (tag.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        absolute = urllib.parse.urljoin(base_url, href)
        parsed = urllib.parse.urlparse(absolute)
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            continue
        # Drop the fragment - `#section` on an otherwise-identical URL
        # isn't a distinct link worth checking a second time.
        normalized = parsed._replace(fragment="").geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        links.append(normalized)
        if len(links) >= limit:
            break
    return links


async def _check_one_link(sem: asyncio.Semaphore, session: aiohttp.ClientSession, link: str) -> dict:
    async with sem:
        try:
            is_up, status = await check_url(session, link)
            return {"url": link, "ok": is_up, "status_code": status, "error": None}
        except UnsafeHostError:
            return {"url": link, "ok": False, "status_code": None, "error": "internal_or_reserved_address"}
        except asyncio.TimeoutError:
            return {"url": link, "ok": False, "status_code": None, "error": "timeout"}
        except aiohttp.ClientResponseError as e:
            return {"url": link, "ok": False, "status_code": e.status, "error": "error_response"}
        except aiohttp.ClientError:
            return {"url": link, "ok": False, "status_code": None, "error": "unreachable"}
        except Exception:
            logger.exception(
                "💥 Unexpected error checking link during SEO audit: %s", _redact_url_credentials(link)
            )
            return {"url": link, "ok": False, "status_code": None, "error": "check_failed"}


async def _check_broken_links(session: aiohttp.ClientSession, links: list[str]) -> dict:
    total_found = len(links)
    checked_links = links[:_MAX_LINKS_CHECKED]
    if not checked_links:
        return {
            "findings": [_finding("broken_links", True, "No links found to check", None)],
            "checked": 0,
            "total_links_found": total_found,
            "details": [],
        }

    sem = asyncio.Semaphore(_BROKEN_LINK_CONCURRENCY)
    tasks = [asyncio.create_task(_check_one_link(sem, session, link)) for link in checked_links]

    # Global wall-clock budget across the WHOLE phase (not stacked per
    # link) - a straggler (slow/rate-limited target, retry/backoff cycle
    # inside `check_url()`) degrades to "skipped, time budget exceeded"
    # instead of blocking the others or extending this Tier-1 endpoint's
    # synchronous response time unboundedly.
    done, pending = await asyncio.wait(tasks, timeout=_BROKEN_LINK_BUDGET_SECONDS)
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)

    results = [task.result() for task in done]
    skipped = len(pending)
    broken = [r for r in results if not r["ok"]]
    checked_count = len(results)

    if broken:
        message = f"{len(broken)} of {checked_count} checked link(s) appear broken or unreachable"
        suggestion = "Fix or remove broken links: " + ", ".join(
            _redact_url_credentials(r["url"]) for r in broken[:5]
        )
    else:
        message = f"All {checked_count} checked link(s) are reachable" if checked_count else "No links were checked"
        suggestion = None
    if skipped:
        message += f" ({skipped} link(s) skipped - time budget exceeded)"

    return {
        "findings": [_finding("broken_links", not broken, message, suggestion)],
        "checked": checked_count,
        "total_links_found": total_found,
        "details": [
            {
                "url": _redact_url_credentials(r["url"]),
                "ok": r["ok"],
                "status_code": r["status_code"],
                "error": r["error"],
            }
            for r in results
        ],
    }


# ---------------------------------------------------------------------------
# Sitemap.xml presence
# ---------------------------------------------------------------------------

async def _check_sitemap(session: aiohttp.ClientSession, origin: str) -> dict:
    sitemap_url = f"{origin}/sitemap.xml"
    try:
        # `check_url()` performs its own `assert_host_is_safe()` on both
        # the initial host and every redirect hop - reused here rather
        # than reimplemented, per the module-boundary decision in the
        # approved feature-spec.
        is_up, status = await check_url(session, sitemap_url)
        found = is_up and status == 200
        message = "sitemap.xml found" if found else f"sitemap.xml not found (status {status})"
        suggestion = None if found else "Add a sitemap.xml and submit it to Google Search Console."
        return {"findings": [_finding("sitemap_present", found, message, suggestion)]}
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt checking sitemap.xml at: %s", _redact_url_credentials(sitemap_url))
        return {"findings": [_finding("sitemap_present", False, _SSRF_BLOCKED_MESSAGE, None)]}
    except asyncio.TimeoutError:
        return {
            "findings": [_finding(
                "sitemap_present", False, "sitemap.xml check timed out",
                "Add a sitemap.xml and submit it to Google Search Console.",
            )]
        }
    except aiohttp.ClientError:
        return {
            "findings": [_finding(
                "sitemap_present", False, "sitemap.xml not found or unreachable",
                "Add a sitemap.xml and submit it to Google Search Console.",
            )]
        }


# ---------------------------------------------------------------------------
# Page speed - reuses `web_tools.py::speed_test`'s aiohttp TraceConfig
# approach (via `_fetch_main_page` above) rather than calling Google
# PageSpeed Insights, so this endpoint has no `GOOGLE_API_KEY` dependency.
# ---------------------------------------------------------------------------

_PAGE_LOAD_PASS_MS = 3000
_TTFB_PASS_MS = 800


def _analyze_page_speed(page: dict) -> dict:
    if not page["reachable"]:
        return {
            "findings": [
                _finding(
                    "page_load_time", False,
                    f"Could not measure page speed: {page['error']}",
                    None,
                )
            ],
            "metrics": None,
        }

    total_ms = page["total_time_ms"]
    ttfb_ms = page["ttfb_ms"]

    total_ok = total_ms is not None and total_ms <= _PAGE_LOAD_PASS_MS
    ttfb_ok = ttfb_ms is not None and ttfb_ms <= _TTFB_PASS_MS

    findings = [
        _finding(
            "total_load_time", total_ok,
            f"Total page load time was {total_ms} ms" if total_ms is not None
            else "Total page load time could not be measured",
            None if total_ok else (
                "Aim for a total load time under 3 seconds - consider optimizing server "
                "response time, image sizes, and reducing redirects."
            ),
        ),
        _finding(
            "time_to_first_byte", ttfb_ok,
            f"Time to first byte was {ttfb_ms} ms" if ttfb_ms is not None
            else "Time to first byte could not be measured",
            None if ttfb_ok else (
                "Improve server response time (TTFB) - consider caching, a CDN, or a "
                "faster backend."
            ),
        ),
    ]

    metrics = {
        "dns_time_ms": page["dns_time_ms"],
        "connect_time_ms": page["connect_time_ms"],
        "ttfb_ms": ttfb_ms,
        "total_time_ms": total_ms,
        "status_code": page["status_code"],
        "content_size_bytes": page["content_size_bytes"],
    }
    return {"findings": findings, "metrics": metrics}


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

async def run_seo_audit(raw_url: str) -> dict:
    """Runs the full 6-area SEO audit for `raw_url`.

    Raises:
        ValueError: empty URL / unparseable hostname - callers (router)
            map this to `HTTPException(400, ...)`, matching every other
            `seo_tools.py` endpoint's convention.
    """
    raw_url = (raw_url or "").strip()
    if not raw_url:
        raise ValueError("URL is required")

    url = raw_url if re.match(r"^https?://", raw_url, re.IGNORECASE) else f"https://{raw_url}"
    parsed = urllib.parse.urlparse(url)
    hostname = parsed.hostname
    if not hostname:
        raise ValueError("Invalid URL format")

    try:
        await assert_host_is_safe(hostname)
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt for SEO audit: %s", hostname)
        return _blocked_report(raw_url)

    origin = f"{parsed.scheme}://{parsed.netloc}"

    page = await _fetch_main_page(url)

    async with aiohttp.ClientSession(timeout=_LINK_CHECK_TIMEOUT) as session:
        sitemap_result = await _check_sitemap(session, origin)

        if page["reachable"]:
            soup = BeautifulSoup(page["html"], "html.parser")
            meta_result = _analyze_meta_tags(soup)
            heading_result = _analyze_headings(soup)
            image_result = _analyze_images(soup)
            links = _extract_links(soup, page["final_url"])
            broken_links_result = await _check_broken_links(session, links)
        else:
            unreachable = _unreachable_block(page["error"] or "Page could not be fetched")
            meta_result = unreachable
            heading_result = unreachable
            image_result = unreachable
            broken_links_result = {**unreachable, "checked": 0, "total_links_found": 0, "details": []}

    speed_result = _analyze_page_speed(page)

    logger.info("SEO audit completed for %s (reachable=%s)", hostname, page["reachable"])
    return {
        "url": raw_url,
        "final_url": page["final_url"],
        "reachable": page["reachable"],
        "error": page["error"],
        "meta_tags": meta_result,
        "heading_structure": heading_result,
        "broken_links": broken_links_result,
        "sitemap": sitemap_result,
        "image_alt_text": image_result,
        "page_speed": speed_result,
    }
