import asyncio
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
from bson import ObjectId
from cryptography import x509
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.security import verify_api_key
from app.models.web_tools import URLEncodeRequest
from app.services.files.service import UploadValidationError, get_file_by_id, save_text_input
from app.services.jobs.service import create_job, mark_failed, mark_queued
from app.shared.network_security import UnsafeHostError, assert_host_is_safe
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
    except ValueError:
        return url
    if not parsed.username and not parsed.password:
        return url
    host = parsed.hostname or ""
    netloc = f"{host}:{parsed.port}" if parsed.port else host
    return parsed._replace(netloc=netloc).geturl()


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
    logger.debug("🔍 Validating URL: %s", request.url)
    url_pattern = re.compile(
        r"^(https?://)?([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(/[\w\-\./?%&=]*)?$",
        re.IGNORECASE
    )
    if not request.url:
        logger.error("❌ URL is required")
        raise HTTPException(status_code=400, detail="URL is required")
    if not url_pattern.match(request.url):
        logger.error("❌ Invalid URL format: %s", request.url)
        raise HTTPException(status_code=400, detail="Invalid URL format")
    try:
        async with aiohttp.ClientSession() as session:
            is_valid, status = await check_url(session, request.url)
            return {"url": request.url, "is_valid": is_valid, "status_code": status}
    except UnsafeHostError:
        logger.warning("🚫 Blocked SSRF attempt validating URL: %s", request.url)
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