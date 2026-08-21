"""`image` module routes (Handbook Part C.3).

`test`/`upload` are Tier 1 - synchronous, no job queue. `ocr`/`compress` are
Tier 2 (Part I.2): each creates a Job, enqueues the matching ARQ task
(`app/worker.py`), and returns immediately - callers poll `GET /jobs/{id}`
(`app/routers/jobs.py`) for the result. This mirrors `app/routers/pdf.py`
exactly - `/image/ocr`'s request contract changes from a raw multipart
upload to `{"file_id": "..."}` referencing a file already uploaded via
`POST /files/upload` or `POST /image/upload`; there is no live frontend
consumer of it yet.

`file_info`/`color_palette` are Tier 1 - single-shot, in-memory analysis of
an uploaded image. Unlike `/image/upload` these do NOT persist a `files`
record or write to disk (Handbook Part C.9: no metadata worth keeping for a
read-only analysis) - they reuse
`app/services/files/service.py`'s `read_and_validate_upload` for the same
extension/size/magic-byte checks `save_uploaded_file` applies, without the
disk write + Mongo insert that follow it there, then hand the validated
bytes to `app/services/image/analysis.py`.
"""
import logging
from typing import Optional

from bson import ObjectId
from fastapi import APIRouter, Depends, File, Request, UploadFile
from pydantic import BaseModel

from app.core.security import verify_api_key
from app.services.files.service import (
    IMAGE_ALLOWED_EXTENSIONS,
    UploadValidationError,
    get_file_by_id,
    read_and_validate_upload,
    sanitize_filename,
    save_uploaded_file,
)
from app.services.image.analysis import get_color_palette, get_image_info
from app.services.jobs.service import create_job, mark_failed, mark_queued
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/image", tags=["Image Tools"])

_VALID_COMPRESSION_LEVELS = {"low", "medium", "high"}


class FileIdRequest(BaseModel):
    file_id: str


class CompressRequest(BaseModel):
    file_id: str
    level: str = "medium"


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


async def _read_uploaded_image(file: UploadFile) -> bytes:
    """Shared by `/image/file_info` and `/image/color_palette` - validates
    the upload (extension/size/magic-bytes) via
    `app/services/files/service.py`'s `read_and_validate_upload` without
    writing anything to disk or Mongo, since both callers only need a
    single-shot in-memory read. Uses the same `sanitize_filename` allow-list
    convention `save_uploaded_file` applies, for consistency (code-reviewer
    note) even though `safe_name` here is only used for the extension check,
    never to construct a filesystem path.
    """
    safe_name = sanitize_filename(file.filename)
    return await read_and_validate_upload(file, safe_name, IMAGE_ALLOWED_EXTENSIONS)


@router.post("/file_info", summary="Get an image's size, dimensions, format, and color mode")
async def image_file_info(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    """Tier 1, instant (Handbook Part I.2) - no Job, no persisted `files` record."""
    try:
        content = await _read_uploaded_image(file)
    except UploadValidationError as e:
        logger.warning("Image file_info upload rejected: %s", e.message)
        raise api_error(e.status_code, e.message, e.error_code)

    try:
        info = await get_image_info(content)
    except ValueError as e:
        # Fixed, hardcoded message rather than `str(e)` - decouples this
        # client-facing contract from Pillow's text (issue #39 - Batch 5 of
        # the #27/#29/#31 error leak class).
        logger.warning("Rejected corrupt image upload for file_info: %s", str(e))
        raise api_error(400, "File is not a valid image", "FILE_INVALID_CONTENT")
    except Exception as e:
        logger.exception("Unexpected error analyzing image for file_info: %s", str(e))
        raise api_error(500, "Failed to analyze image", "IMAGE_ANALYSIS_FAILED")

    logger.info("Analyzed image for file_info (%d bytes)", len(content))
    return envelope(True, "Image analyzed", data=info)


@router.post("/color_palette", summary="Extract the dominant color palette from an image")
async def image_color_palette(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    """Tier 1, instant (Handbook Part I.2) - no Job, no persisted `files` record."""
    try:
        content = await _read_uploaded_image(file)
    except UploadValidationError as e:
        logger.warning("Image color_palette upload rejected: %s", e.message)
        raise api_error(e.status_code, e.message, e.error_code)

    try:
        colors = await get_color_palette(content)
    except ValueError as e:
        # Same client-safe-error convention as `image_file_info` above.
        logger.warning("Rejected corrupt image upload for color_palette: %s", str(e))
        raise api_error(400, "File is not a valid image", "FILE_INVALID_CONTENT")
    except Exception as e:
        logger.exception("Unexpected error extracting color palette: %s", str(e))
        raise api_error(500, "Failed to analyze image", "IMAGE_ANALYSIS_FAILED")

    logger.info("Extracted color palette from image (%d bytes)", len(content))
    return envelope(True, "Color palette extracted", data={"colors": colors})


async def _create_image_job(
    request: Request, file_id: str, job_type: str, api_key: dict, params: Optional[dict] = None
) -> dict:
    file_doc = await get_file_by_id(file_id)
    if file_doc is None:
        raise api_error(404, "File not found or has expired", "FILE_NOT_FOUND")

    owner_id = str(api_key["key_data"]["_id"])
    if str(file_doc.ownerApiKeyId) != owner_id:
        raise api_error(403, "Not authorized to use this file", "FILE_FORBIDDEN")

    ext = "." + file_doc.originalFilename.rsplit(".", 1)[-1].lower() if "." in file_doc.originalFilename else ""
    if ext not in IMAGE_ALLOWED_EXTENSIONS:
        raise api_error(400, "File must be a supported image (.jpg, .jpeg, .png)", "FILE_INVALID_TYPE")

    job = await create_job(
        file_doc.id, job_type, ObjectId(api_key["key_data"]["_id"]), params=params
    )
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


@router.post("/compress", summary="Compress an image to reduce file size (async job)")
async def compress_image(payload: CompressRequest, request: Request, api_key: dict = Depends(verify_api_key)):
    """Single-file Tier 2 tool, same shape as `/ocr`. `level` (default
    "medium") is threaded to the worker via the job's generic `params`
    field the same way `/pdf/split` threads `ranges` (see
    `app/routers/pdf.py`'s docstring / `app/schemas/job.py`'s
    `JobBase.params`).
    """
    if payload.level not in _VALID_COMPRESSION_LEVELS:
        raise api_error(400, "level must be one of: low, medium, high", "LEVEL_INVALID")

    data = await _create_image_job(
        request, payload.file_id, "image_compress", api_key, params={"level": payload.level}
    )
    return envelope(True, "Compression job created", data=data)
