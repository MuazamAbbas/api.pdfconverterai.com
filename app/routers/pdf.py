"""`pdf` module routes (Handbook Part C.3).

`test`/`upload` are Tier 1 - synchronous, no job queue. `convert`/
`to_word`/`summarize` are Tier 2 (Part I.2): each creates a Job, enqueues
the matching ARQ task (`app/worker.py`), and returns immediately - callers
poll `GET /jobs/{id}` (`app/routers/jobs.py`) for the result. This is an
intentional, spec-approved breaking change to those three endpoints' request
contract (multipart upload -> `{"file_id": "..."}` referencing a file
already uploaded via `POST /files/upload` or `POST /pdf/upload`) - there is
no live frontend consumer of them yet.
"""
import logging

from bson import ObjectId
from fastapi import APIRouter, Depends, Request, UploadFile, File
from pydantic import BaseModel

from app.core.security import verify_api_key
from app.services.files.service import UploadValidationError, get_file_by_id, save_uploaded_file
from app.services.jobs.service import create_job, mark_failed, mark_queued
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/pdf", tags=["PDF Tools"])


class FileIdRequest(BaseModel):
    file_id: str


@router.get("/test", summary="Test PDF Tools endpoint")
async def test_pdf(api_key: dict = Depends(verify_api_key)):
    return envelope(True, "PDF Tools router is working")


@router.post("/upload", summary="Upload a PDF file")
async def upload_pdf(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    """Kept working from a caller's perspective (still a multipart PDF
    upload, still Tier 1) - internally now calls the same shared upload
    function `app/routers/files.py` uses, so the upload also gets a real
    `files` record instead of being written to disk and immediately deleted.
    """
    if not file.filename or not file.filename.lower().endswith(".pdf"):
        logger.warning("Upload rejected, not a PDF: %s", file.filename)
        raise api_error(400, "File must be a PDF", "FILE_INVALID_TYPE")
    try:
        owner_id = ObjectId(api_key["key_data"]["_id"])
        file_doc = await save_uploaded_file(file, owner_id)
    except UploadValidationError as e:
        logger.warning("PDF upload rejected: %s", e.message)
        raise api_error(e.status_code, e.message, e.error_code)
    except Exception as e:
        logger.exception("PDF upload failed: %s", str(e))
        raise api_error(500, "Failed to upload file", "UPLOAD_FAILED")

    logger.info("PDF uploaded: id=%s", file_doc.id)
    return envelope(True, "File uploaded", data={"file_id": str(file_doc.id), "filename": file_doc.originalFilename})


async def _create_pdf_job(request: Request, file_id: str, job_type: str, api_key: dict) -> dict:
    file_doc = await get_file_by_id(file_id)
    if file_doc is None:
        raise api_error(404, "File not found or has expired", "FILE_NOT_FOUND")

    owner_id = str(api_key["key_data"]["_id"])
    if str(file_doc.ownerApiKeyId) != owner_id:
        raise api_error(403, "Not authorized to use this file", "FILE_FORBIDDEN")

    if not file_doc.originalFilename.lower().endswith(".pdf"):
        raise api_error(400, "File must be a PDF", "FILE_INVALID_TYPE")

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


@router.post("/convert", summary="Convert PDF to text (async job)")
async def convert_pdf(payload: FileIdRequest, request: Request, api_key: dict = Depends(verify_api_key)):
    data = await _create_pdf_job(request, payload.file_id, "pdf_convert", api_key)
    return envelope(True, "Conversion job created", data=data)


@router.post("/to_word", summary="Convert PDF to Word (async job)")
async def pdf_to_word(payload: FileIdRequest, request: Request, api_key: dict = Depends(verify_api_key)):
    data = await _create_pdf_job(request, payload.file_id, "pdf_to_word", api_key)
    return envelope(True, "Word conversion job created", data=data)


@router.post("/summarize", summary="Summarize PDF content (async job)")
async def summarize_pdf(payload: FileIdRequest, request: Request, api_key: dict = Depends(verify_api_key)):
    data = await _create_pdf_job(request, payload.file_id, "pdf_summarize", api_key)
    return envelope(True, "Summarization job created", data=data)
