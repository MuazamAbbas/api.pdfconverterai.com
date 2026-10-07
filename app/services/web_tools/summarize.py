import asyncio
import logging
import urllib.parse

import aiohttp
from bs4 import BeautifulSoup

from app.shared.network_security import UnsafeHostError, assert_host_is_safe
from app.shared.web.pinned_resolver import SafeResolver
from app.shared.web.redirect_fetch import (
    _MAX_REDIRECT_HOPS,
    _REDIRECT_STATUSES,
    _redact_url_credentials,
)

logger = logging.getLogger(__name__)

# Caps the response body read before it's handed to BeautifulSoup (Handbook
# Part C.10) - `await response.content.read()` previously (pre-this-change,
# via `response.text()`) read the whole body unbounded, a resource-exhaustion
# vector for a caller-supplied URL. Matches `app/services/seo/seo_audit.py::
# _MAX_HTML_BYTES` (same reasoning: plenty for text extraction, caps an
# unexpectedly huge remote response).
_MAX_BODY_BYTES = 2_000_000

# `StreamReader.read(n)` is NOT "read n bytes or EOF" - it returns as soon as
# *any* data is in the buffer, which can be far fewer than `n` bytes for a
# chunked-transfer-encoded or TCP-segmented response (i.e. almost every real
# webpage). A single `read(_MAX_BODY_BYTES + 1)` call therefore often
# returned only the first chunk, silently under-reading real pages instead
# of reading up to the cap. `_read_body_capped()` below loops fixed-size
# `_READ_CHUNK_SIZE` reads until EOF (`b""`) or until the accumulated total
# exceeds `_MAX_BODY_BYTES`, at which point it stops immediately rather than
# draining the rest of an oversized stream.
_READ_CHUNK_SIZE = 65_536

# Single shared deadline (Handbook Part C.10) for the *entire* fetch - every
# redirect hop plus the final body read - not a per-hop budget that could
# compound up to `_MAX_REDIRECT_HOPS` x this value. Replaces the prior
# per-session `ClientTimeout(total=20)`, which aiohttp re-applies fresh to
# each individual `session.get()` call rather than to the whole operation.
_FETCH_TIMEOUT_SECONDS = 30

# Client-safe, generic message for a blocked SSRF attempt - deliberately
# distinct from "no text extracted" (see `WebpageFetchBlockedError` below)
# so a blocked fetch isn't confusingly reported as the page just being thin
# on text. No hostnames/IPs/internal details, matching the fixed-message
# convention `app/services/web_tools/processors.py::execute()` already uses
# for the "no text extracted" ValueError (issue #39).
_FETCH_BLOCKED_MESSAGE = "This webpage could not be fetched"


class WebpageFetchBlockedError(ValueError):
    """Raised by `fetch_webpage_text()` when the target URL (or a redirect
    hop it followed) resolves to a disallowed private/loopback/link-local/
    reserved/multicast/metadata address (Handbook Part C.10, closing
    `api.pdfconverterai.com#98`).

    Subclasses `ValueError` so any caller that only ever catches `ValueError`
    (the existing contract `fetch_webpage_text()` already documented before
    this change) still catches this without modification. `app/services/
    web_tools/processors.py::WebToolsSummarizeProcessor.execute()` catches
    this specific type *before* its existing generic `except ValueError` so
    a blocked SSRF attempt gets its own distinct, generic client-facing
    message instead of being folded into "no text extracted from the
    webpage" - order matters there, not here.
    """


def _safe_connector() -> aiohttp.TCPConnector:
    """`TCPConnector` wired to `SafeResolver` (Handbook Part C.10 /
    `api.pdfconverterai.com#53`) - mirrors `app/routers/web_tools.py`'s and
    `app/services/seo/seo_audit.py`'s own `_safe_connector()` helpers
    exactly. Every `aiohttp.ClientSession` this module builds for a
    caller-supplied URL uses one of these, so the connector's own real DNS
    resolution (not just the pre-request `assert_host_is_safe()` check) is
    validated atomically at the moment of connecting - closing the
    DNS-rebinding TOCTOU window between a separate check and a separate,
    later connect.
    """
    return aiohttp.TCPConnector(resolver=SafeResolver())


async def _read_body_capped(response: aiohttp.ClientResponse) -> str:
    """Reads `response`'s body in fixed `_READ_CHUNK_SIZE` chunks until EOF
    or until the accumulated total exceeds `_MAX_BODY_BYTES`, whichever
    comes first - see `_READ_CHUNK_SIZE`'s module-level docstring for why a
    single bare `.read(_MAX_BODY_BYTES + 1)` call is not sufficient. Stops
    reading immediately once over cap rather than draining the rest of an
    oversized remote stream."""
    chunks: list[bytes] = []
    total = 0
    while total <= _MAX_BODY_BYTES:
        chunk = await response.content.read(_READ_CHUNK_SIZE)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)

    body = b"".join(chunks)
    if total > _MAX_BODY_BYTES:
        logger.debug("Summarize fetch: response body truncated at %d bytes", _MAX_BODY_BYTES)
    return body[:_MAX_BODY_BYTES].decode("utf-8", errors="replace")


async def _fetch_safe(session: aiohttp.ClientSession, url: str) -> str:
    """SSRF-guarded fetch that returns the final response body as text,
    capped at `_MAX_BODY_BYTES` (see `_read_body_capped()`).

    Mirrors `app/shared/web/redirect_fetch.py::check_url()`'s manual
    redirect-following loop (resolve-and-validate the starting host,
    `allow_redirects=False` on every real request with an explicit loop
    instead, re-validating every redirect hop's target host via
    `assert_host_is_safe()` before following it, bounded by
    `_MAX_REDIRECT_HOPS`) - kept local to this module (not added to
    `check_url()` itself, which only ever returns `(bool, status)`, no body)
    so `web_tools.py`'s/`seo_audit.py`'s existing callers and tests of
    `check_url()` are untouched.

    One `session` (and therefore one connector/resolver) is reused across
    every hop here, matching `check_url()`'s/`pinned_resolver.py`'s
    documented pattern exactly - not rebuilt per hop. This still re-pins on
    every hop that matters: a redirect to a *different* host is always a
    DNS-cache miss on the connector, so `SafeResolver.resolve()` runs fresh
    for it; a repeat of the *same* host within the connector's DNS-cache TTL
    reuses the IP already validated for that host, which introduces no new
    rebinding window since no new, independently re-resolvable lookup
    happens for it.

    No per-call timeout is passed to `session.get()` here - the caller
    (`fetch_webpage_text`) wraps this whole function in a single
    `asyncio.wait_for(..., _FETCH_TIMEOUT_SECONDS)` covering every hop plus
    the body read, rather than each hop getting its own fresh budget.

    Raises:
        UnsafeHostError: the starting host or a redirect hop targets a
            disallowed address.
        aiohttp.ClientResponseError: a non-2xx/3xx final response (via
            `raise_for_status()`), or `aiohttp.TooManyRedirects` if the hop
            cap is exceeded - both `aiohttp.ClientError` subclasses, which
            `WebToolsSummarizeProcessor.execute()` already classifies as
            transient/retryable.
    """
    hostname = urllib.parse.urlparse(url).hostname
    if hostname:
        await assert_host_is_safe(hostname)

    current_url = url
    for _hop in range(_MAX_REDIRECT_HOPS + 1):
        async with session.get(current_url, allow_redirects=False) as response:
            location = response.headers.get("Location")
            if response.status in _REDIRECT_STATUSES and location:
                next_url = urllib.parse.urljoin(current_url, location)
                next_hostname = urllib.parse.urlparse(next_url).hostname
                if next_hostname:
                    # Raises UnsafeHostError on an unsafe redirect target -
                    # not caught here, propagates to the caller exactly like
                    # the pre-request check above.
                    await assert_host_is_safe(next_hostname)
                logger.debug(
                    "↪️ Summarize fetch following redirect: %s -> %s",
                    _redact_url_credentials(current_url), _redact_url_credentials(next_url),
                )
                current_url = next_url
                continue

            response.raise_for_status()
            return await _read_body_capped(response)

    logger.warning("⚠️ Too many redirects fetching webpage to summarize: %s", _redact_url_credentials(url))
    raise aiohttp.TooManyRedirects(
        request_info=response.request_info,
        history=response.history,
        status=response.status,
        message="Too Many Redirects",
    )


async def fetch_webpage_text(url: str) -> str:
    """Fetch `url` and extract its `<p>` text, truncated to 1000 chars.

    Split out from the summarization step (unlike the prior
    `Summarizer.summarize_webpage`, which did fetch+extract+summarize in one
    method) so `app/services/web_tools/processors.py`'s
    `WebToolsSummarizeProcessor.execute()` can classify failures precisely
    (ADR-003): a network hiccup (`aiohttp.ClientError`) is transient/
    retryable, while no text extracted / text too short is a permanent,
    non-retryable input problem.

    SSRF-guarded (Handbook Part C.10, closing `api.pdfconverterai.com#98`):
    resolves/connects via `_safe_connector()`'s `SafeResolver` (pinned,
    DNS-rebinding-proof) and follows redirects manually via `_fetch_safe()`,
    re-validating every hop. A blocked target raises
    `WebpageFetchBlockedError` (a `ValueError` subclass) rather than letting
    `UnsafeHostError` reach the caller directly.

    Args:
        url (str): Webpage URL. Must start with `http://`/`https://`.

    Returns:
        str: Extracted `<p>` text, truncated to 1000 characters (unchanged
            from the prior implementation).

    Raises:
        ValueError: URL missing the http(s):// prefix, no text extracted,
            or extracted text is under 50 characters.
        WebpageFetchBlockedError: the URL (or a redirect hop) targets a
            disallowed private/loopback/link-local/metadata address.
        aiohttp.ClientError: Network/HTTP failure fetching the page.
    """
    if not url.startswith(("http://", "https://")):
        logger.error("Invalid URL: %s", url)
        raise ValueError("URL must start with http:// or https://")

    logger.debug("Fetching URL: %s", url)
    try:
        async with aiohttp.ClientSession(connector=_safe_connector()) as session:
            content = await asyncio.wait_for(
                _fetch_safe(session, url), timeout=_FETCH_TIMEOUT_SECONDS
            )
    except UnsafeHostError as e:
        logger.warning(
            "🚫 Blocked SSRF attempt fetching webpage to summarize: %s",
            urllib.parse.urlparse(url).hostname,
        )
        raise WebpageFetchBlockedError(_FETCH_BLOCKED_MESSAGE) from e
    except TimeoutError as e:
        # One shared ~`_FETCH_TIMEOUT_SECONDS` deadline for the whole
        # operation (every redirect hop plus the body read) tripped -
        # `asyncio.wait_for` raises the builtin `TimeoutError` (which
        # `asyncio.TimeoutError` is an alias for since Python 3.11). Re-raise
        # as `aiohttp.ServerTimeoutError`, which multiply-inherits both
        # `aiohttp.ClientError` and `TimeoutError`, so
        # `WebToolsSummarizeProcessor.execute()`'s existing
        # `except aiohttp.ClientError` -> transient/retryable classification
        # catches it unchanged - no change needed there.
        logger.warning(
            "⏱️ Timed out fetching webpage to summarize (> %ds): %s",
            _FETCH_TIMEOUT_SECONDS, _redact_url_credentials(url),
        )
        raise aiohttp.ServerTimeoutError(f"Timed out fetching webpage after {_FETCH_TIMEOUT_SECONDS}s") from e

    soup = BeautifulSoup(content, "html.parser")
    paragraphs = soup.find_all("p")
    text = " ".join(p.get_text().strip() for p in paragraphs if p.get_text().strip())
    logger.debug("Extracted text length: %d", len(text))
    if not text:
        logger.error("No text extracted from URL: %s", url)
        raise ValueError("No text extracted from webpage")
    if len(text) < 50:
        logger.error("Text too short: %d characters", len(text))
        raise ValueError("Text must be at least 50 characters")
    return text[:1000]


async def summarize_webpage_text(text: str, pipeline) -> str:
    """Summarize already-extracted webpage text using a preloaded t5-small pipeline.

    Args:
        text (str): Extracted webpage text (see `fetch_webpage_text`).
        pipeline: Preloaded Hugging Face `summarization` pipeline
            (t5-small), loaded once per worker process in `app/worker.py`'s
            `on_startup` hook and passed in via `ctx["summarize_pipeline"]`
            (Handbook Part C.2/C.4, ADR-003/ADR-006) - the same shared
            pipeline `app/services/text/summarize.py`'s
            `summarize_text_service` uses, intentionally (previously reached
            via a `Summarizer(app)` instance in the FastAPI request
            process; that path is now Tier 2, so the pipeline lives in the
            ARQ worker process's `ctx` instead).

    Returns:
        str: Summarized text.
    """
    summary = pipeline(text, max_length=150, min_length=30, do_sample=False)[0]["summary_text"]
    logger.debug("Summary generated, length: %d", len(summary))
    return summary
