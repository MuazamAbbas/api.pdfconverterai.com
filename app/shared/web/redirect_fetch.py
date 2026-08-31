"""Shared, SSRF-guarded HTTP redirect-following primitive (Handbook Part
C.10 / D.3 - `app/shared/` holds code reused across router modules without
routers importing each other directly, which the one-module-one-
responsibility rule (Part C.3) forbids).

Relocated here from `app/routers/web_tools.py` (pure move, no behavior
change - see docs/roadmap/SPRINT_STATUS.md 2026-08-31 "feature-spec
approved: SEO Audit" entry) because `app/routers/seo_tools.py`'s new
`seo_audit` endpoint needs the same SSRF-guarded, bounded-redirect-hop
`check_url()` primitive that `web_tools.py`'s `website_down_detector`/
`validate_url`/`speed_test` already use. Reusing router-local code across
routers would violate the "one module = one responsibility" rule (Handbook
Part C.3) - this module is the shared home for it instead.

`web_tools.py` imports `check_url`/`_MAX_REDIRECT_HOPS`/`_REDIRECT_STATUSES`/
`_redact_url_credentials` back from here so its existing callers
(`website_down_detector`, `validate_url`, `speed_test`) keep working
unchanged.
"""
import logging
import urllib.parse

import aiohttp
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.shared.network_security import assert_host_is_safe

logger = logging.getLogger(__name__)

# Redirect hops `check_url()` will follow manually before giving up - matches
# typical browser/requests defaults, bounded rather than unlimited so a
# redirect loop degrades to a clean "too many redirects" result instead of
# spinning forever.
_MAX_REDIRECT_HOPS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def _redact_url_credentials(url: str) -> str:
    """Strips embedded userinfo (`user:pass@host`) from a URL before it is
    logged (Handbook Part C.10: logging must never capture credentials/
    secrets). No-op for URLs that don't carry userinfo, so the common case
    logs exactly what it did before this change."""
    try:
        parsed = urllib.parse.urlparse(url)
        if not parsed.username and not parsed.password:
            return url
        host = parsed.hostname or ""
        netloc = f"{host}:{parsed.port}" if parsed.port else host
        return parsed._replace(netloc=netloc).geturl()
    except ValueError:
        # Covers both a malformed `urlparse()` call itself and a malformed
        # port (e.g. `http://user:pass@host:abc/path`) - `.port` is a lazy
        # property on the parse result that raises ValueError separately
        # from `urlparse()`, so it must live inside this same try block to
        # degrade to the same "couldn't parse, return as-is" fallback.
        return url


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=1, max=10),
    retry=retry_if_exception_type(aiohttp.ClientResponseError),
    reraise=True,
    before_sleep=lambda retry_state: logger.debug("Retrying URL validation: attempt %d", retry_state.attempt_number)
)
async def check_url(session: aiohttp.ClientSession, url: str) -> tuple[bool, int]:
    """SSRF-guarded (Handbook Part C.10): resolves the target hostname and
    rejects private/loopback/link-local/reserved/multicast addresses before
    connecting - retrofitted here (not just on the new endpoints below)
    because both `validate_url` and `website_down_detector` share this one
    function, and there was previously no SSRF protection anywhere on this
    path. Raises `UnsafeHostError`, which is not in `retry_if_exception_type`
    above, so it propagates immediately instead of being retried.

    Redirects are followed manually (`allow_redirects=False` on the actual
    request, with an explicit loop below) rather than delegating to aiohttp's
    own `allow_redirects=True`, specifically so every redirect hop's target
    host is re-validated against `assert_host_is_safe()` before it's
    followed - otherwise a plain public URL that 302s to
    `http://169.254.169.254/...` (or any other internal address) would
    bypass the guard above entirely, since that guard only ever checked the
    original hostname. Bounded to `_MAX_REDIRECT_HOPS` hops; exceeding that
    raises `aiohttp.TooManyRedirects` (a `ClientResponseError` subclass),
    matching what aiohttp itself would raise for the equivalent
    `allow_redirects=True` case - callers already handle `ClientResponseError`
    generically, so no new except clause was needed for this."""
    hostname = urllib.parse.urlparse(url).hostname
    if hostname:
        await assert_host_is_safe(hostname)

    current_url = url
    for _hop in range(_MAX_REDIRECT_HOPS + 1):
        async with session.get(current_url, allow_redirects=False, timeout=5) as response:
            status = response.status
            location = response.headers.get("Location")
            if status in _REDIRECT_STATUSES and location:
                next_url = urllib.parse.urljoin(current_url, location)
                next_hostname = urllib.parse.urlparse(next_url).hostname
                if next_hostname:
                    # Raises UnsafeHostError on an unsafe redirect target -
                    # not caught here, propagates to the caller exactly like
                    # the pre-request check above.
                    await assert_host_is_safe(next_hostname)
                logger.debug(
                    "↪️ Following redirect: %s -> %s, status: %d",
                    _redact_url_credentials(current_url), _redact_url_credentials(next_url), status
                )
                current_url = next_url
                continue
            safe_current_url = _redact_url_credentials(current_url)
            if 200 <= status < 400:
                logger.debug("✅ URL is reachable: %s, status: %d", safe_current_url, status)
                return True, status
            elif status == 429:
                logger.warning("⚠️ Rate limit hit for URL: %s, status: %d", safe_current_url, status)
                raise aiohttp.ClientResponseError(
                    request_info=response.request_info,
                    history=response.history,
                    status=status,
                    message="Too Many Requests"
                )
            else:
                logger.error("❌ URL is not reachable: %s, status: %d", safe_current_url, status)
                return False, status

    logger.warning("⚠️ Too many redirects for URL: %s", _redact_url_credentials(url))
    raise aiohttp.TooManyRedirects(
        request_info=response.request_info,
        history=response.history,
        status=response.status,
        message="Too Many Redirects",
    )
