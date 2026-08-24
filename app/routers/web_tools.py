import asyncio
import ipaddress
import logging
import re
import socket
import ssl
import time
import urllib.parse
from datetime import datetime, timezone

import aiohttp
import dns.asyncresolver
import dns.exception
import dns.resolver
import whois
from bson import ObjectId
from cryptography import x509
from fastapi import APIRouter, Depends, HTTPException, Request
from ipwhois import IPWhois
from ipwhois.exceptions import BaseIpwhoisException, IPDefinedError
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from whois.parser import WhoisEntry
from whois.whois import NICClient

from app.core.security import verify_api_key
from app.models.web_tools import IPLookupRequest, SpeedTestRequest, URLEncodeRequest, WhoisLookupRequest
from app.services.files.service import UploadValidationError, get_file_by_id, save_text_input
from app.services.jobs.service import create_job, mark_failed, mark_queued
from app.shared.network_security import UnsafeHostError, assert_host_is_safe, assert_host_is_safe_sync
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web_tools", tags=["Web Tools"])

DNS_RECORD_TYPES = ("A", "AAAA", "MX", "TXT", "NS", "CNAME")

_SSRF_BLOCKED_MESSAGE = "Cannot check internal or reserved network addresses"

# Redirect hops `check_url()` will follow manually before giving up - matches
# typical browser/requests defaults, bounded rather than unlimited so a
# redirect loop degrades to a clean "too many redirects" result instead of
# spinning forever.
_MAX_REDIRECT_HOPS = 5
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

_CONNECT_ERROR_MESSAGES = {
    "timeout": "Connection to the domain timed out",
    "refused": "Connection to the domain was refused",
    "dns": "Could not resolve the domain",
    "connection_failed": "Unable to establish a TLS connection on port 443",
}

_VERIFY_ERROR_MESSAGES = {
    "hostname_mismatch": "Certificate hostname does not match the domain",
    "self_signed": "Certificate is self-signed",
    "expired": "Certificate has expired",
    "verification_failed": "Certificate could not be verified",
    "handshake_failed": "TLS handshake failed",
}


class URLRequest(BaseModel):
    url: str


class URLUploadRequest(BaseModel):
    url: str


class FileIdRequest(BaseModel):
    file_id: str


class DomainRequest(BaseModel):
    domain: str


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


def _extract_hostname(value: str) -> str | None:
    """Tolerates both a bare domain (`example.com`) and a full URL
    (`https://example.com/path`) - matches v1 tool tolerance, since users
    routinely paste a full URL into a "domain" field."""
    value = (value or "").strip()
    if not value:
        return None
    candidate = value if "//" in value else f"//{value}"
    hostname = urllib.parse.urlparse(candidate).hostname
    return hostname.lower() if hostname else None


@router.get("/test", summary="Test Web Tools endpoint")
async def test_web_tools(api_key: dict = Depends(verify_api_key)):
    logger.debug("🧪 Testing Web Tools endpoint")
    return {"message": "Web Tools router is working"}

@router.post("/url_encode", summary="Encode a URL")
async def url_encode(request: URLEncodeRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔧 Encoding URL: %s", request.url)
    try:
        encoded_url = urllib.parse.quote(request.url)
        logger.debug("✅ URL encoded: %s", encoded_url)
        return {"original_url": request.url, "encoded_url": encoded_url}
    except Exception as e:
        logger.exception("💥 Error encoding URL: %s", str(e))
        raise api_error(500, "Failed to encode URL", "URL_ENCODE_FAILED")

@router.post("/upload", summary="Upload a URL for webpage summarization jobs")
async def upload_web_tools(payload: URLUploadRequest, api_key: dict = Depends(verify_api_key)):
    """Tier 1 - writes `url` to disk and registers a `files` record for it,
    the same way `app/routers/pdf.py`'s `upload_pdf` does for a real upload,
    so `POST /web_tools/summarize` can reference it by `file_id` (Handbook
    Part C.3/C.5). Uses the standard `envelope`/`api_error` response style
    (not this file's other, older raw-dict endpoints) since this is a new
    Job-System endpoint, consistent with `pdf`/`image`'s Tier 2 endpoints.
    """
    if not payload.url.startswith(("http://", "https://")):
        logger.warning("URL upload rejected, missing http(s):// prefix: %s", payload.url)
        raise api_error(400, "URL must start with http:// or https://", "URL_INVALID")

    try:
        owner_id = ObjectId(api_key["key_data"]["_id"])
        file_doc = await save_text_input(payload.url, owner_id, "url_input.txt")
    except UploadValidationError as e:
        logger.warning("URL upload rejected: %s", e.message)
        raise api_error(e.status_code, e.message, e.error_code)
    except Exception as e:
        logger.exception("URL upload failed: %s", str(e))
        raise api_error(500, "Failed to upload URL", "UPLOAD_FAILED")

    logger.info("URL uploaded: id=%s", file_doc.id)
    return envelope(True, "URL uploaded", data={"file_id": str(file_doc.id), "filename": file_doc.originalFilename})


async def _create_web_tools_job(request: Request, file_id: str, job_type: str, api_key: dict) -> dict:
    """Mirrors `app/routers/pdf.py`'s `_create_pdf_job` / `app/routers/image.py`'s
    `_create_image_job` (Handbook Part C.4): ownership check, create the Job,
    enqueue the matching ARQ task, transition Pending -> Queued.
    """
    file_doc = await get_file_by_id(file_id)
    if file_doc is None:
        raise api_error(404, "File not found or has expired", "FILE_NOT_FOUND")

    owner_id = str(api_key["key_data"]["_id"])
    if str(file_doc.ownerApiKeyId) != owner_id:
        raise api_error(403, "Not authorized to use this file", "FILE_FORBIDDEN")

    job = await create_job(file_doc.id, job_type, ObjectId(api_key["key_data"]["_id"]))
    try:
        await request.app.state.arq_redis.enqueue_job(job_type, str(job.id), _job_id=str(job.id))
        await mark_queued(str(job.id))
    except Exception as e:
        logger.exception("Failed to enqueue job %s (%s): %s", job.id, job_type, str(e))
        await mark_failed(str(job.id), "Failed to queue job for processing")
        raise api_error(503, "Job queue is temporarily unavailable", "QUEUE_UNAVAILABLE")

    logger.info("Created job %s (%s) for file %s", job.id, job_type, file_id)
    return {"job_id": str(job.id), "status": "queued"}


@router.post("/summarize", summary="Summarize webpage content (async job)")
async def webpage_summarize(payload: FileIdRequest, request: Request, api_key: dict = Depends(verify_api_key)):
    data = await _create_web_tools_job(request, payload.file_id, "web_tools_summarize", api_key)
    return envelope(True, "Summarization job created", data=data)

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

@router.post("/validate_url", summary="Validate URL and check if it is reachable")
async def validate_url(request: URLRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Validating URL: %s", _redact_url_credentials(request.url))
    url_pattern = re.compile(
        r"^(https?://)?([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(/[\w\-\./?%&=]*)?$",
        re.IGNORECASE
    )
    if not request.url:
        logger.error("❌ URL is required")
        raise HTTPException(status_code=400, detail="URL is required")
    if not url_pattern.match(request.url):
        logger.error("❌ Invalid URL format: %s", _redact_url_credentials(request.url))
        raise HTTPException(status_code=400, detail="Invalid URL format")
    try:
        async with aiohttp.ClientSession() as session:
            is_valid, status = await check_url(session, request.url)
            return {"url": request.url, "is_valid": is_valid, "status_code": status}
    except UnsafeHostError:
        logger.warning(
            "🚫 Blocked SSRF attempt validating URL: %s", _redact_url_credentials(request.url)
        )
        return {
            "url": request.url,
            "is_valid": False,
            "status_code": None,
            "error": _SSRF_BLOCKED_MESSAGE,
        }
    except aiohttp.ClientResponseError as e:
        logger.exception("💥 Client response error validating URL: %s", str(e))
        return {
            "url": request.url,
            "is_valid": False,
            "status_code": e.status,
            "error": "The URL returned an error response",
        }
    except aiohttp.ClientError as e:
        logger.exception("💥 Client error validating URL: %s", str(e))
        return {
            "url": request.url,
            "is_valid": False,
            "status_code": None,
            "error": "Unable to reach the URL",
        }
    except Exception as e:
        logger.exception("💥 Error validating URL: %s", str(e))
        raise api_error(500, "Failed to validate URL", "URL_VALIDATION_FAILED")


# ---------------------------------------------------------------------------
# Website Down Detector
#
# `validate_url` (above) answers "is this a well-formed URL AND is it
# reachable" - a format-validity check with a reachability side effect.
# "Website Down Detector" is a differently-shaped tool: the caller already
# has a URL/domain in hand and wants an uptime verdict plus diagnostics
# (response time, status code, when checked), not a format check. Rather
# than duplicate the retry-wrapped GET, this reuses `check_url()` for the
# actual reachability primitive and exposes its own down-detector-shaped
# response contract on top of it.
# ---------------------------------------------------------------------------

def _down_detector_result(
    url: str,
    is_up: bool,
    status_code: int | None,
    response_time_ms: float | None,
    checked_at: str,
    error: str | None,
) -> dict:
    return envelope(
        True,
        "Website status checked",
        data={
            "url": url,
            "is_up": is_up,
            "status_code": status_code,
            "response_time_ms": response_time_ms,
            "checked_at": checked_at,
            "error": error,
        },
    )


@router.post("/website_down_detector", summary="Check whether a website is up or down")
async def website_down_detector(request: URLRequest, api_key: dict = Depends(verify_api_key)):
    raw_url = (request.url or "").strip()
    logger.debug("🔍 Checking website status: %s", _redact_url_credentials(raw_url))
    if not raw_url:
        logger.warning("❌ Website down detector rejected: URL is required")
        raise api_error(400, "URL is required", "URL_REQUIRED")

    url = raw_url if re.match(r"^https?://", raw_url, re.IGNORECASE) else f"https://{raw_url}"
    hostname = urllib.parse.urlparse(url).hostname
    if not hostname:
        logger.warning(
            "❌ Website down detector rejected: invalid URL format: %s",
            _redact_url_credentials(raw_url),
        )
        raise api_error(400, "Invalid URL format", "URL_INVALID")

    checked_at = datetime.now(timezone.utc).isoformat()

    try:
        await assert_host_is_safe(hostname)
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt for website down detector: %s", hostname)
        return _down_detector_result(raw_url, False, None, None, checked_at, _SSRF_BLOCKED_MESSAGE)

    start = time.monotonic()
    try:
        async with aiohttp.ClientSession() as session:
            is_up, status = await check_url(session, url)
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        error = None if is_up else "Website returned a non-success status code"
        return _down_detector_result(raw_url, is_up, status, elapsed_ms, checked_at, error)
    except aiohttp.ClientResponseError as e:
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        logger.info("Website down detector: %s returned an error status", hostname)
        error = "The website returned an error response"
        return _down_detector_result(raw_url, False, e.status, elapsed_ms, checked_at, error)
    except asyncio.TimeoutError:
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        logger.info("Website down detector: %s timed out", hostname)
        error = "Website did not respond in time"
        return _down_detector_result(raw_url, False, None, elapsed_ms, checked_at, error)
    except aiohttp.ClientError:
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        logger.info("Website down detector: %s is unreachable", hostname)
        error = "Website is unreachable"
        return _down_detector_result(raw_url, False, None, elapsed_ms, checked_at, error)
    except UnsafeHostError:
        # A redirect hop inside check_url() targeted a disallowed internal/
        # private address - same verdict as the pre-request guard above,
        # just discovered mid-request instead of up front.
        elapsed_ms = round((time.monotonic() - start) * 1000, 2)
        logger.warning(
            "🚫 Blocked SSRF attempt for website down detector (redirect hop): %s", hostname
        )
        return _down_detector_result(
            raw_url, False, None, elapsed_ms, checked_at, _SSRF_BLOCKED_MESSAGE
        )
    except Exception as e:
        logger.exception("💥 Unexpected error checking website status for %s: %s", hostname, str(e))
        raise api_error(500, "Failed to check website status", "WEBSITE_DOWN_DETECTOR_FAILED")


# ---------------------------------------------------------------------------
# DNS Lookup
# ---------------------------------------------------------------------------

async def _query_dns_record(
    resolver: "dns.asyncresolver.Resolver", domain: str, rtype: str
) -> tuple[list[str], bool]:
    """Returns (values, is_nxdomain). Zero records of a given type is not an
    error (e.g. a domain with no MX records) - only a genuine NXDOMAIN means
    the domain doesn't exist at all, and that's the only case the caller
    treats as a real error; every other failure (NoAnswer/timeout/no
    nameservers/etc.) degrades to an empty list."""
    try:
        answer = await resolver.resolve(domain, rtype, lifetime=5)
    except dns.resolver.NXDOMAIN:
        return [], True
    except Exception as e:
        logger.debug("DNS %s lookup for %s returned no result: %s", rtype, domain, str(e))
        return [], False

    values: list[str] = []
    for rdata in answer:
        if rtype == "TXT":
            text = "".join(
                part.decode("utf-8", errors="replace") if isinstance(part, bytes) else str(part)
                for part in rdata.strings
            )
            values.append(text)
        else:
            values.append(rdata.to_text().rstrip("."))
    return values, False


@router.post("/dns_lookup", summary="Look up DNS records for a domain")
async def dns_lookup(request: DomainRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Looking up DNS records for: %s", request.domain)
    domain = _extract_hostname(request.domain)
    if not domain:
        logger.warning("❌ DNS lookup rejected: domain is required")
        raise api_error(400, "Domain is required", "DOMAIN_REQUIRED")

    empty_records = {t: [] for t in DNS_RECORD_TYPES}

    try:
        await assert_host_is_safe(domain)
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt for DNS lookup: %s", domain)
        return envelope(True, "DNS lookup completed", data={
            "domain": domain,
            "records": empty_records,
            "error": _SSRF_BLOCKED_MESSAGE,
        })

    resolver = dns.asyncresolver.Resolver()
    resolver.timeout = 5
    resolver.lifetime = 5

    records: dict[str, list[str]] = dict(empty_records)
    try:
        first_type = DNS_RECORD_TYPES[0]
        first_values, first_is_nxdomain = await _query_dns_record(resolver, domain, first_type)
        if first_is_nxdomain:
            logger.info("DNS lookup NXDOMAIN for domain: %s", domain)
            return envelope(True, "DNS lookup completed", data={
                "domain": domain,
                "records": empty_records,
                "error": "Domain does not exist",
            })
        records[first_type] = first_values

        for rtype in DNS_RECORD_TYPES[1:]:
            values, _ = await _query_dns_record(resolver, domain, rtype)
            records[rtype] = values
    except Exception as e:
        logger.exception("💥 Unexpected error during DNS lookup for %s: %s", domain, str(e))
        raise api_error(500, "Failed to perform DNS lookup", "DNS_LOOKUP_FAILED")

    logger.info("DNS lookup completed for domain: %s", domain)
    return envelope(
        True, "DNS lookup completed", data={"domain": domain, "records": records, "error": None}
    )


# ---------------------------------------------------------------------------
# SSL Checker
# ---------------------------------------------------------------------------

def _classify_verify_error(exc: ssl.SSLCertVerificationError) -> str:
    message = (getattr(exc, "verify_message", "") or str(exc)).lower()
    if "hostname mismatch" in message or "does not match" in message or "doesn't match" in message:
        return "hostname_mismatch"
    if "self signed" in message or "self-signed" in message:
        return "self_signed"
    if "expired" in message:
        return "expired"
    return "verification_failed"


def _fetch_certificate_der(hostname: str, port: int = 443, timeout: float = 5.0) -> dict:
    """Blocking (socket/ssl) - always run via an executor, never awaited
    directly. Two-phase: first attempts a fully-verified connection (trust
    chain + hostname) - if that succeeds we have our answer. If it fails on
    certificate verification specifically (self-signed/expired/hostname
    mismatch/untrusted CA), reconnect once more without verification just to
    still retrieve and report the certificate's own details - a self-signed
    or expired cert is a legitimate check *result* for this tool, not an
    internal failure, same reasoning as `check_url` above. Connection-level
    failures (refused/timeout/DNS/no TLS at all on that port) short-circuit
    with no second attempt and no certificate to report.
    """
    try:
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            ctx = ssl.create_default_context()
            try:
                with ctx.wrap_socket(sock, server_hostname=hostname) as ssock:
                    der = ssock.getpeercert(binary_form=True)
                return {"der": der, "verified": True, "verify_error": None, "connect_error": None}
            except ssl.SSLCertVerificationError as e:
                verify_error = _classify_verify_error(e)
            except ssl.SSLError:
                verify_error = "handshake_failed"
    except (socket.timeout, TimeoutError):
        return {"der": None, "verified": False, "verify_error": None, "connect_error": "timeout"}
    except ConnectionRefusedError:
        return {"der": None, "verified": False, "verify_error": None, "connect_error": "refused"}
    except socket.gaierror:
        return {"der": None, "verified": False, "verify_error": None, "connect_error": "dns"}
    except OSError:
        return {
            "der": None, "verified": False,
            "verify_error": None, "connect_error": "connection_failed",
        }

    # Verification-specific failure above - reconnect once more, unverified,
    # just to retrieve the certificate's own details for reporting.
    try:
        with socket.create_connection((hostname, port), timeout=timeout) as sock:
            ctx2 = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            ctx2.check_hostname = False
            ctx2.verify_mode = ssl.CERT_NONE
            with ctx2.wrap_socket(sock, server_hostname=hostname) as ssock:
                der = ssock.getpeercert(binary_form=True)
        return {"der": der, "verified": False, "verify_error": verify_error, "connect_error": None}
    except OSError:
        return {
            "der": None, "verified": False,
            "verify_error": verify_error, "connect_error": "connection_failed",
        }


def _x509_name_to_dict(name: "x509.Name") -> dict:
    result = {}
    for attr in name:
        try:
            key = attr.oid._name
        except AttributeError:
            key = attr.oid.dotted_string
        result[key] = attr.value
    return result


def _empty_ssl_result(domain: str, error: str) -> dict:
    return envelope(True, "SSL check completed", data={
        "domain": domain,
        "valid": False,
        "issuer": None,
        "subject": None,
        "valid_from": None,
        "valid_until": None,
        "days_until_expiry": None,
        "is_self_signed": None,
        "error": error,
    })


@router.post("/ssl_checker", summary="Check an SSL/TLS certificate for a domain")
async def ssl_checker(request: DomainRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Checking SSL certificate for: %s", request.domain)
    domain = _extract_hostname(request.domain)
    if not domain:
        logger.warning("❌ SSL check rejected: domain is required")
        raise api_error(400, "Domain is required", "DOMAIN_REQUIRED")

    try:
        await assert_host_is_safe(domain)
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt for SSL check: %s", domain)
        return _empty_ssl_result(domain, _SSRF_BLOCKED_MESSAGE)

    try:
        loop = asyncio.get_running_loop()
        result = await loop.run_in_executor(None, _fetch_certificate_der, domain)
    except Exception as e:
        logger.exception("💥 Unexpected error checking SSL certificate for %s: %s", domain, str(e))
        raise api_error(500, "Failed to check SSL certificate", "SSL_CHECK_FAILED")

    if result["der"] is None:
        error_message = _CONNECT_ERROR_MESSAGES.get(
            result["connect_error"], "Unable to check the SSL certificate"
        )
        logger.info("SSL check failed for %s: %s", domain, result["connect_error"])
        return _empty_ssl_result(domain, error_message)

    try:
        cert = x509.load_der_x509_certificate(result["der"])
        issuer = _x509_name_to_dict(cert.issuer)
        subject = _x509_name_to_dict(cert.subject)
        valid_from = cert.not_valid_before_utc
        valid_until = cert.not_valid_after_utc
        now = datetime.now(timezone.utc)
        is_expired = now > valid_until
        days_until_expiry = (valid_until - now).days
        is_self_signed = (cert.issuer == cert.subject) or (result["verify_error"] == "self_signed")
    except Exception as e:
        logger.exception("💥 Failed to parse SSL certificate for %s: %s", domain, str(e))
        raise api_error(500, "Failed to check SSL certificate", "SSL_CHECK_FAILED")

    is_valid = bool(result["verified"]) and not is_expired

    error_message = None
    if not is_valid:
        if is_expired:
            error_message = "Certificate has expired"
        elif result["verify_error"]:
            error_message = _VERIFY_ERROR_MESSAGES.get(
                result["verify_error"], "Certificate could not be verified"
            )
        else:
            error_message = "Certificate could not be verified"

    logger.info("SSL check completed for %s: valid=%s", domain, is_valid)
    return envelope(True, "SSL check completed", data={
        "domain": domain,
        "valid": is_valid,
        "issuer": issuer,
        "subject": subject,
        "valid_from": valid_from.isoformat(),
        "valid_until": valid_until.isoformat(),
        "days_until_expiry": days_until_expiry,
        "is_self_signed": is_self_signed,
        "error": error_message,
    })


# ---------------------------------------------------------------------------
# WHOIS Lookup
#
# Free-tier replacement for v1's paid-`WHOISXML_API_KEY`-backed tool
# (Handbook CLAUDE.md deferred-key list) - uses `python-whois`'s raw port-43
# socket lookup instead, per founder decision to close this v1-parity item
# without the deferred key. No geolocation/paid-data dependency at all.
# ---------------------------------------------------------------------------

def _normalize_whois_date(value) -> str | None:
    """`python-whois` returns a single `datetime`, a list of `datetime`s
    (some registries repeat a date field across multiple matched lines), or
    occasionally a plain string it couldn't cast to a date at all -
    normalize all three shapes to one ISO-8601 string (or the raw string if
    it was never parsed). For a list, the earliest value is used - this
    only ever fires for `creation_date` in practice (the field the "avoid
    duplicates" dedup in the library's own parser most often still leaves
    as a multi-entry list), and "earliest" matches "first registered"
    semantics; `expiration_date` degrades the same way for consistency even
    though a list is not observed for it in practice.
    """
    if value is None:
        return None
    if isinstance(value, list):
        candidates = [v for v in value if v is not None]
        if not candidates:
            return None
        if all(isinstance(v, datetime) for v in candidates):
            return min(candidates).isoformat()
        return str(candidates[0])
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _normalize_whois_name_servers(value) -> list[str]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    seen: set[str] = set()
    result: list[str] = []
    for entry in values:
        if not entry:
            continue
        lowered = str(entry).strip().lower().rstrip(".")
        if lowered and lowered not in seen:
            seen.add(lowered)
            result.append(lowered)
    return result


def _empty_whois_result(domain: str, error: str) -> dict:
    return envelope(True, "WHOIS lookup completed", data={
        "domain": domain,
        "registrar": None,
        "creation_date": None,
        "expiration_date": None,
        "name_servers": [],
        "registrant_org": None,
        "error": error,
    })


class _SafeWhoisSocket(socket.socket):
    """A `socket.socket` whose `connect()` is SSRF-guarded (Handbook Part
    C.10) before it's allowed to actually connect - see `_SafeNICClient`
    below for why this exists and exactly what it closes."""

    def connect(self, address):
        host = address[0] if isinstance(address, tuple) else address
        assert_host_is_safe_sync(host)
        return super().connect(address)


class _SafeNICClient(NICClient):
    """`whois.whois.NICClient`, except every socket it opens is a
    `_SafeWhoisSocket` - closes a real SSRF gap: `whois.whois()` never
    just connects to the caller-supplied domain's own address (already
    covered by this router's `assert_host_is_safe(domain)` pre-check). It
    also connects to a TLD-mapped whois host chosen internally by the
    library, and then - by default, since the recursion flag is on unless
    `WHOIS_QUICK` is passed - regex-extracts a `"Whois Server: <host>"`
    value straight out of that first response's text and opens a second,
    completely unvalidated `socket.connect((nhost, 43))` to it (see
    `NICClient.whois()` / `findwhois_server()` in
    `.venv/Lib/site-packages/whois/whois.py`). Both hops go through
    `get_socket()` -> `.connect()`, so overriding at that one boundary
    closes the gap for both, without needing to reimplement or hook into
    the library's recursion/parsing logic itself.

    Does not support the library's optional SOCKS-proxy mode (`SOCKS` env
    var, see the parent class's own `get_socket()`) - this backend never
    sets it, and silently falling back to an unguarded plain socket for
    that path would defeat the point of this override, so it's dropped
    rather than preserved.
    """

    def get_socket(self):
        return _SafeWhoisSocket(socket.AF_INET, socket.SOCK_STREAM)


def _safe_whois_lookup(domain: str, flags: int = 0, timeout: int = 10):
    """Equivalent to `whois.whois()`'s own default code path (builtin
    socket client - the SOCKS and native-`whois`-command paths are not
    used by this endpoint), except driven through `_SafeNICClient` instead
    of the library's own unguarded `NICClient` (see above), so this is
    what `whois_lookup()` below calls instead of `whois.whois()` directly.
    `flags=0` is the library's own default and leaves recursive WHOIS
    lookups enabled - registrar-level data (for gTLDs that split registry/
    registrar WHOIS) is preserved, unlike disabling recursion via
    `WHOIS_QUICK` would trade away.
    """
    nic_client = _SafeNICClient()
    idna_domain = domain.encode("idna").decode("utf-8")
    text = nic_client.whois_lookup(None, idna_domain, flags, timeout=timeout)
    if not text:
        raise whois.exceptions.WhoisError("Whois command returned no output")
    return WhoisEntry.load(idna_domain, text)


@router.post("/whois_lookup", summary="Look up WHOIS registration data for a domain")
async def whois_lookup(request: WhoisLookupRequest, api_key: dict = Depends(verify_api_key)):
    logger.debug("🔍 Looking up WHOIS data for: %s", request.domain)
    domain = _extract_hostname(request.domain)
    if not domain:
        logger.warning("❌ WHOIS lookup rejected: domain is required")
        raise api_error(400, "Domain is required", "DOMAIN_REQUIRED")

    try:
        await assert_host_is_safe(domain)
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt for WHOIS lookup: %s", domain)
        return _empty_whois_result(domain, _SSRF_BLOCKED_MESSAGE)

    try:
        result = await asyncio.to_thread(_safe_whois_lookup, domain)
    except (whois.exceptions.PywhoisError, UnicodeError):
        # PywhoisError covers "no WHOIS server for this TLD", "domain not
        # registered" on registries that respond with an explicit not-found
        # message, and the library's own internal socket-error/empty-output
        # fallback (`ignore_socket_errors=True` by default, so most raw
        # connection failures surface as this rather than a raw OSError).
        # UnicodeError covers a hostname that fails IDNA encoding (e.g. a
        # malformed label) - both are "no data for this input", not a bug.
        logger.info("WHOIS lookup: no data found for %s", domain)
        return _empty_whois_result(domain, "No WHOIS data found for this domain")
    except UnsafeHostError:
        # The registrar-referral hop `_safe_whois_lookup()` followed
        # pointed at a disallowed internal/private address - same verdict
        # as the pre-lookup guard above, just discovered mid-lookup by
        # `_SafeWhoisSocket` instead of up front.
        logger.warning(
            "🚫 Blocked SSRF attempt for WHOIS lookup (referral host): %s", domain
        )
        return _empty_whois_result(domain, _SSRF_BLOCKED_MESSAGE)
    except Exception as e:
        logger.exception("💥 Unexpected error during WHOIS lookup for %s: %s", domain, str(e))
        raise api_error(500, "Failed to perform WHOIS lookup", "WHOIS_LOOKUP_FAILED")

    registrar = result.get("registrar")
    creation_date = _normalize_whois_date(result.get("creation_date"))
    expiration_date = _normalize_whois_date(result.get("expiration_date"))
    name_servers = _normalize_whois_name_servers(result.get("name_servers"))
    # `org` is the registrant organization field - commonly `None`/redacted
    # for gTLDs post-2018 ICANN GDPR privacy policy. That is an expected,
    # legitimate result, not folded into the "no data at all" check below.
    registrant_org = result.get("org")

    # Some TLDs/registries return a parsed object with every other field
    # `None` instead of raising - the same "no data" outcome as the
    # exception path above (e.g. an unregistered domain on a WHOIS server
    # that still answers with an empty record), just a different shape.
    # `registrant_org` is included here too (despite the GDPR-redaction
    # rationale above meaning its *absence* never counts against "has
    # data") so that the rare record with only `registrant_org` populated
    # is correctly treated as real, if sparse, data - not silently
    # discarded in favor of the canned empty result.
    if not any([registrar, creation_date, expiration_date, name_servers, registrant_org]):
        logger.info("WHOIS lookup: empty record for %s", domain)
        return _empty_whois_result(domain, "No WHOIS data found for this domain")

    logger.info("WHOIS lookup completed for domain: %s", domain)
    return envelope(True, "WHOIS lookup completed", data={
        "domain": domain,
        "registrar": registrar,
        "creation_date": creation_date,
        "expiration_date": expiration_date,
        "name_servers": name_servers,
        "registrant_org": registrant_org,
        "error": None,
    })


# ---------------------------------------------------------------------------
# IP Address Lookup
#
# Scope note: this deliberately returns ONLY ownership/ASN information
# (via free RDAP lookups through `ipwhois`), never geolocation (city/
# lat-long/timezone). v1's paid-`GOOGLE_API_KEY`-backed version returned
# geolocation too - this free-tier replacement does not, by founder
# decision, not as a limitation discovered after the fact.
# ---------------------------------------------------------------------------

def _empty_ip_lookup_result(ip: str, error: str) -> dict:
    return envelope(True, "IP lookup completed", data={
        "ip": ip,
        "asn": None,
        "asn_description": None,
        "network_name": None,
        "network_cidr": None,
        "country": None,
        "abuse_contact": None,
        "error": error,
    })


def _extract_abuse_contact(objects: dict | None) -> str | None:
    """RDAP entity shapes vary a lot by RIR - `objects` is a handle -> entity
    dict, each entity optionally carrying a `roles` list and a `contact`
    dict whose `email` is itself a list of `{type, value}` dicts (see
    `ipwhois.rdap._RDAPContact._parse_email`). Returns the first email found
    on the first entity with an `abuse` role, or `None` if any level of that
    shape is missing - absence is a normal, expected result for many
    networks, not an error."""
    if not objects:
        return None
    for entity in objects.values():
        roles = entity.get("roles") or []
        if "abuse" not in roles:
            continue
        contact = entity.get("contact") or {}
        emails = contact.get("email") or []
        for email in emails:
            value = email.get("value") if isinstance(email, dict) else email
            if value:
                return value
    return None


# `IPWhois.lookup_rdap()`'s own defaults (`retry_count=3`,
# `rate_limit_timeout=120`) can block a thread-pool worker for minutes on a
# rate-limited RIR RDAP server - `get_http_json()` sleeps up to
# `rate_limit_timeout` seconds before each retry (see
# `.venv/Lib/site-packages/ipwhois/net.py`). Tightened here so a single
# retry/rate-limit cycle is bounded to a few seconds, with
# `_IP_LOOKUP_TOTAL_TIMEOUT` below as a hard backstop in case the library's
# own bounds don't behave as expected.
_IP_LOOKUP_RETRY_COUNT = 1
_IP_LOOKUP_RATE_LIMIT_TIMEOUT = 3
_IP_LOOKUP_TOTAL_TIMEOUT = 12


@router.post("/ip_lookup", summary="Look up ownership/ASN information for an IP address")
async def ip_lookup(request: IPLookupRequest, api_key: dict = Depends(verify_api_key)):
    raw_ip = (request.ip or "").strip()
    logger.debug("🔍 Looking up IP address: %s", raw_ip)
    if not raw_ip:
        logger.warning("❌ IP lookup rejected: IP is required")
        raise api_error(400, "IP address is required", "IP_REQUIRED")

    try:
        ipaddress.ip_address(raw_ip)
    except ValueError:
        logger.warning("❌ IP lookup rejected: invalid IP address: %s", raw_ip)
        raise api_error(400, "A valid IP address is required", "IP_INVALID")

    try:
        # `assert_host_is_safe` resolves via `getaddrinfo()`, which resolves
        # an IP literal to itself - the same private/loopback/reserved
        # check as a DNS-mediated SSRF attempt applies directly here.
        await assert_host_is_safe(raw_ip)
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt for IP lookup: %s", raw_ip)
        return _empty_ip_lookup_result(raw_ip, _SSRF_BLOCKED_MESSAGE)

    try:
        result = await asyncio.wait_for(
            asyncio.to_thread(
                lambda: IPWhois(raw_ip).lookup_rdap(
                    retry_count=_IP_LOOKUP_RETRY_COUNT,
                    rate_limit_timeout=_IP_LOOKUP_RATE_LIMIT_TIMEOUT,
                )
            ),
            timeout=_IP_LOOKUP_TOTAL_TIMEOUT,
        )
    except IPDefinedError:
        logger.info("IP lookup: no ownership data for reserved address %s", raw_ip)
        return _empty_ip_lookup_result(raw_ip, "No ownership data available for this address")
    except asyncio.TimeoutError:
        # Backstop only - `asyncio.to_thread()`'s worker thread can't
        # actually be force-killed once `wait_for()` gives up on it, so a
        # rare worst-case call may keep running in the background after
        # this returns. Acceptable: it's bounded (loosely) by the tight
        # retry/rate-limit settings above already, this is just insurance
        # against the library not honoring them as expected.
        logger.info("IP lookup: RDAP lookup timed out for %s", raw_ip)
        return _empty_ip_lookup_result(raw_ip, "IP lookup timed out, please try again later")
    except BaseIpwhoisException as e:
        # Covers the RDAP/ASN network-failure exceptions `ipwhois` itself
        # raises (rate-limited, HTTP lookup failed, ASN registry/whois
        # lookup failed, etc.) - a transient lookup failure is a
        # legitimate check *result* here, the same reasoning
        # `check_url()`/`ssl_checker()` elsewhere in this file already
        # apply to their own connection-level failures, not an internal
        # error worth a 500.
        logger.info("IP lookup: RDAP lookup failed for %s: %s", raw_ip, str(e))
        return _empty_ip_lookup_result(
            raw_ip, "Unable to complete IP lookup right now, please try again later"
        )
    except Exception as e:
        logger.exception("💥 Unexpected error during IP lookup for %s: %s", raw_ip, str(e))
        raise api_error(500, "Failed to look up IP address", "IP_LOOKUP_FAILED")

    network = result.get("network") or {}
    logger.info("IP lookup completed for: %s", raw_ip)
    return envelope(True, "IP lookup completed", data={
        "ip": raw_ip,
        "asn": result.get("asn"),
        "asn_description": result.get("asn_description"),
        "network_name": network.get("name"),
        "network_cidr": network.get("cidr"),
        "country": result.get("asn_country_code"),
        "abuse_contact": _extract_abuse_contact(result.get("objects")),
        "error": None,
    })


# ---------------------------------------------------------------------------
# Website Speed Test
# ---------------------------------------------------------------------------

_SPEED_TEST_TIMEOUT = aiohttp.ClientTimeout(total=5)
_TOO_MANY_REDIRECTS_MESSAGE = "Too many redirects"


def _empty_speed_test_result(url: str, error: str) -> dict:
    return envelope(True, "Speed test completed", data={
        "url": url,
        "dns_time_ms": None,
        "connect_time_ms": None,
        "ttfb_ms": None,
        "total_time_ms": None,
        "status_code": None,
        "content_size_bytes": None,
        "error": error,
    })


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


@router.post("/speed_test", summary="Run a website load-speed test")
async def speed_test(request: SpeedTestRequest, api_key: dict = Depends(verify_api_key)):
    raw_url = (request.url or "").strip()
    logger.debug("🔍 Running speed test for: %s", _redact_url_credentials(raw_url))
    if not raw_url:
        logger.warning("❌ Speed test rejected: URL is required")
        raise api_error(400, "URL is required", "URL_REQUIRED")

    url = raw_url if re.match(r"^https?://", raw_url, re.IGNORECASE) else f"https://{raw_url}"
    hostname = urllib.parse.urlparse(url).hostname
    if not hostname:
        logger.warning("❌ Speed test rejected: invalid URL format: %s", _redact_url_credentials(raw_url))
        raise api_error(400, "Invalid URL format", "URL_INVALID")

    try:
        await assert_host_is_safe(hostname)
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt for speed test: %s", hostname)
        return _empty_speed_test_result(raw_url, _SSRF_BLOCKED_MESSAGE)

    timings: dict = {}
    trace_config = _build_speed_trace_config(timings)
    total_start = time.monotonic()

    try:
        async with aiohttp.ClientSession(
            trace_configs=[trace_config], timeout=_SPEED_TEST_TIMEOUT
        ) as session:
            current_url = url
            for _hop in range(_MAX_REDIRECT_HOPS + 1):
                timings.clear()
                async with session.get(current_url, allow_redirects=False) as response:
                    location = response.headers.get("Location")
                    if response.status in _REDIRECT_STATUSES and location:
                        next_url = urllib.parse.urljoin(current_url, location)
                        next_hostname = urllib.parse.urlparse(next_url).hostname
                        if next_hostname:
                            # Re-validated per hop for the same reason
                            # `check_url()` does it - a public URL that
                            # redirects to an internal address must not
                            # bypass the pre-request guard above.
                            await assert_host_is_safe(next_hostname)
                        logger.debug(
                            "↪️ Speed test following redirect: %s -> %s",
                            _redact_url_credentials(current_url), _redact_url_credentials(next_url),
                        )
                        current_url = next_url
                        continue

                    body = await response.read()
                    total_ms = round((time.monotonic() - total_start) * 1000, 2)
                    logger.info("Speed test completed for %s: status=%d", hostname, response.status)
                    return envelope(True, "Speed test completed", data={
                        "url": raw_url,
                        "dns_time_ms": timings.get("dns_time_ms"),
                        "connect_time_ms": timings.get("connect_time_ms"),
                        "ttfb_ms": timings.get("ttfb_ms"),
                        "total_time_ms": total_ms,
                        "status_code": response.status,
                        "content_size_bytes": len(body),
                        "error": None,
                    })

            logger.warning("⚠️ Too many redirects for speed test: %s", _redact_url_credentials(raw_url))
            return _empty_speed_test_result(raw_url, _TOO_MANY_REDIRECTS_MESSAGE)
    except UnsafeHostError:
        # A redirect hop targeted a disallowed internal/private address -
        # discovered mid-request, same verdict as the pre-request guard.
        logger.warning("🚫 Blocked SSRF attempt for speed test (redirect hop): %s", hostname)
        return _empty_speed_test_result(raw_url, _SSRF_BLOCKED_MESSAGE)
    except asyncio.TimeoutError:
        logger.info("Speed test: %s timed out", hostname)
        return _empty_speed_test_result(raw_url, _CONNECT_ERROR_MESSAGES["timeout"])
    except aiohttp.ClientConnectorDNSError:
        logger.info("Speed test: could not resolve %s", hostname)
        return _empty_speed_test_result(raw_url, _CONNECT_ERROR_MESSAGES["dns"])
    except aiohttp.ClientConnectorError:
        logger.info("Speed test: could not connect to %s", hostname)
        return _empty_speed_test_result(raw_url, _CONNECT_ERROR_MESSAGES["connection_failed"])
    except aiohttp.ClientError:
        logger.info("Speed test: %s is unreachable", hostname)
        return _empty_speed_test_result(raw_url, "Website is unreachable")
    except Exception as e:
        logger.exception("💥 Unexpected error running speed test for %s: %s", hostname, str(e))
        raise api_error(500, "Failed to run speed test", "SPEED_TEST_FAILED")