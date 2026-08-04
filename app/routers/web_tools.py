import logging
import re
import urllib.parse

import aiohttp
from bson import ObjectId
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.security import verify_api_key
from app.models.web_tools import URLEncodeRequest
from app.services.files.service import UploadValidationError, get_file_by_id, save_text_input
from app.services.jobs.service import create_job, mark_failed, mark_queued
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/web_tools", tags=["Web Tools"])


class URLRequest(BaseModel):
    url: str


class URLUploadRequest(BaseModel):
    url: str


class FileIdRequest(BaseModel):
    file_id: str


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
    async with session.get(url, allow_redirects=True, timeout=5) as response:
        status = response.status
        if 200 <= status < 400:
            logger.debug("✅ URL is reachable: %s, status: %d", url, status)
            return True, status
        elif status == 429:
            logger.warning("⚠️ Rate limit hit for URL: %s, status: %d", url, status)
            raise aiohttp.ClientResponseError(
                request_info=response.request_info,
                history=response.history,
                status=status,
                message="Too Many Requests"
            )
        else:
            logger.error("❌ URL is not reachable: %s, status: %d", url, status)
            return False, status

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