"""`image` module routes (Handbook Part C.3).

`test`/`upload` are Tier 1 - synchronous, no job queue. `ocr` is Tier 2
(Part I.2): it creates a Job, enqueues the matching ARQ task
(`app/worker.py`), and returns immediately - callers poll `GET /jobs/{id}`
(`app/routers/jobs.py`) for the result. This mirrors `app/routers/pdf.py`
exactly - `/image/ocr`'s request contract changes from a raw multipart
upload to `{"file_id": "..."}` referencing a file already uploaded via
`POST /files/upload` or `POST /image/upload`; there is no live frontend
consumer of it yet.
"""
import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel

from app.core.security import verify_api_key
from app.services.files.service import (
    IMAGE_ALLOWED_EXTENSIONS,
    UploadValidationError,
    get_file_by_id,
    save_uploaded_file,
)
from app.services.jobs.service import create_job, mark_failed, mark_queued
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/image", tags=["Image Tools"])


class FileIdRequest(BaseModel):
    file_id: str


@router.get("/test", summary="Test Image Tools endpoint")
async def test_image(api_key: dict = Depends(verify_api_key)):
    return envelope(True, "Image Tools router is working")


@router.post("/upload", summary="Upload an image file")
async def upload_image(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    """Mirrors `app/routers/pdf.py`'s `upload_pdf` - a Tier 1 multipart
    upload that calls the same shared upload function `app/routers/files.py`
    uses, scoped to the `image` module's own allow-list
    (`IMAGE_ALLOWED_EXTENSIONS`) so this also gets a real `files` record.
    """
    try:
        owner_id = ObjectId(api_key["key_data"]["_id"])
        file_doc = await save_uploaded_file(file, owner_id, allowed_extensions=IMAGE_ALLOWED_EXTENSIONS)
    except UploadValidationError as e:
        logger.warning("Image upload rejected: %s", e.message)
        raise api_error(e.status_code, e.message, e.error_code)
    except Exception as e:
        logger.exception("Image upload failed: %s", str(e))
        raise api_error(500, "Failed to upload file", "UPLOAD_FAILED")

    logger.info("Image uploaded: id=%s", file_doc.id)
    return envelope(True, "File uploaded", data={"file_id": str(file_doc.id), "filename": file_doc.originalFilename})


async def _create_image_job(request: Request, file_id: str, job_type: str, api_key: dict) -> dict:
    file_doc = await get_file_by_id(file_id)
    if file_doc is None:
        raise api_error(404, "File not found or has expired", "FILE_NOT_FOUND")

    owner_id = str(api_key["key_data"]["_id"])
    if str(file_doc.ownerApiKeyId) != owner_id:
        raise api_error(403, "Not authorized to use this file", "FILE_FORBIDDEN")

    ext = "." + file_doc.originalFilename.rsplit(".", 1)[-1].lower() if "." in file_doc.originalFilename else ""
    if ext not in IMAGE_ALLOWED_EXTENSIONS:
        raise api_error(400, "File must be a supported image (.jpg, .jpeg, .png)", "FILE_INVALID_TYPE")

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


@router.post("/ocr", summary="Extract text from image using OCR (async job)")
async def ocr_image(payload: FileIdRequest, request: Request, api_key: dict = Depends(verify_api_key)):
    data = await _create_image_job(request, payload.file_id, "image_ocr", api_key)
    return envelope(True, "OCR job created", data=data)
