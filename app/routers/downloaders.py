import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from app.core.security import verify_api_key
from app.services.files.service import UploadValidationError, get_file_by_id, save_text_input
from app.services.jobs.service import create_job, mark_failed, mark_queued
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/downloaders", tags=["Downloaders"])


class URLUploadRequest(BaseModel):
    url: str


class FileIdRequest(BaseModel):
    file_id: str


@router.get("/test", summary="Test Downloaders endpoint")
async def test_downloaders():
    logger.debug("🧪 Testing Downloaders endpoint")
    return {"message": "Downloaders router is working"}


@router.post("/upload", summary="Upload a URL for downloader jobs")
async def upload_downloaders(payload: URLUploadRequest, api_key: dict = Depends(verify_api_key)):
    """Tier 1 - writes `url` to disk and registers a `files` record for it,
    mirroring `app/routers/web_tools.py`'s `upload_web_tools`, so
    `POST /downloaders/youtube` can reference it by `file_id` (Handbook
    Part C.3/C.5). Uses the standard `envelope`/`api_error` response style.
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
    return envelope(
        True,
        "URL uploaded",
        data={"file_id": str(file_doc.id), "filename": file_doc.originalFilename},
    )


async def _create_downloaders_job(
    request: Request, file_id: str, job_type: str, api_key: dict
) -> dict:
    """Mirrors `app/routers/pdf.py`'s `_create_pdf_job` / `app/routers/web_tools.py`'s
    `_create_web_tools_job` (Handbook Part C.4): ownership check, create the
    Job, enqueue the matching ARQ task, transition Pending -> Queued.
    Duplicated here rather than imported from `web_tools.py` - ADR-015 sets
    `downloaders`/`web_tools` as the eventual *target* module boundary, but
    that merge hasn't physically happened yet, so this file follows the same
    per-router-duplication precedent `pdf`/`image`/`text` already use instead
    of reaching into a sibling router's private helper.
    """
    file_doc = await get_file_by_id(file_id)
    if file_doc is None:
        raise api_error(404, "File not found or has expired", "FILE_NOT_FOUND")

    owner_id = str(api_key["key_data"]["_id"])
    if str(file_doc.ownerApiKeyId) != owner_id:
        raise api_error(403, "Not authorized to use this file", "FILE_FORBIDDEN")

    job = await create_job(file_doc.id, job_type)
    try:
        await request.app.state.arq_redis.enqueue_job(job_type, str(job.id), _job_id=str(job.id))
        await mark_queued(str(job.id))
    except Exception as e:
        logger.exception("Failed to enqueue job %s (%s): %s", job.id, job_type, str(e))
        await mark_failed(str(job.id), "Failed to queue job for processing")
        raise api_error(503, "Job queue is temporarily unavailable", "QUEUE_UNAVAILABLE")

    logger.info("Created job %s (%s) for file %s", job.id, job_type, file_id)
    return {"job_id": str(job.id), "status": "queued"}


@router.post("/youtube", summary="Download YouTube video (async job)")
async def download_youtube(
    payload: FileIdRequest, request: Request, api_key: dict = Depends(verify_api_key)
):
    """Tier 2 - creates a `downloaders_youtube` job and enqueues it via ARQ,
    mirroring `app/routers/web_tools.py`'s `webpage_summarize`. The actual
    yt_dlp download runs in the worker-side Processor, not inline here
    (Handbook Part I.2 - no blocking network/disk I/O in the event loop).
    """
    data = await _create_downloaders_job(request, payload.file_id, "downloaders_youtube", api_key)
    return envelope(True, "YouTube download job created", data=data)
