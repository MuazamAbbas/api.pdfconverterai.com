"""`files` module routes (Handbook Part C.3, Part I.2 Tier 1 - synchronous,
no job queue). Owns upload + download for every module; Tier 2 tools
(pdf/image/ai) never duplicate this - they take a `file_id` produced here
and hand it to `app/routers/jobs.py`/their own module's job-creation logic.
"""
import logging
import os

from bson import ObjectId
from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import FileResponse

from app.core.security import verify_api_key
from app.services.files.service import UploadValidationError, get_file_by_id, save_uploaded_file
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/files", tags=["Files"])


@router.post("/upload", summary="Upload a file")
async def upload_file(file: UploadFile = File(...), api_key: dict = Depends(verify_api_key)):
    if not file.filename:
        logger.warning("Upload rejected: missing filename")
        raise api_error(400, "A filename is required", "FILE_INVALID")
    try:
        owner_id = ObjectId(api_key["key_data"]["_id"])
        file_doc = await save_uploaded_file(file, owner_id)
    except UploadValidationError as e:
        logger.warning("Upload rejected: %s", e.message)
        raise api_error(e.status_code, e.message, e.error_code)
    except Exception as e:
        logger.exception("Upload failed: %s", str(e))
        raise api_error(500, "Failed to upload file", "UPLOAD_FAILED")

    logger.info("File uploaded: id=%s owner=%s", file_doc.id, owner_id)
    return envelope(
        True,
        "File uploaded",
        data={"file_id": str(file_doc.id), "filename": file_doc.originalFilename, "size_bytes": file_doc.sizeBytes},
    )


@router.get("/{file_id}/download", summary="Download a file by id")
async def download_file(file_id: str, api_key: dict = Depends(verify_api_key)):
    file_doc = await get_file_by_id(file_id)
    if file_doc is None:
        logger.warning("Download requested for unknown/expired file: %s", file_id)
        raise api_error(404, "File not found or has expired", "FILE_NOT_FOUND")

    owner_id = str(api_key["key_data"]["_id"])
    if str(file_doc.ownerApiKeyId) != owner_id:
        logger.warning("Download denied: file %s not owned by requesting key", file_id)
        raise api_error(403, "Not authorized to access this file", "FILE_FORBIDDEN")

    if not os.path.exists(file_doc.storagePath):
        logger.error("File record %s has no bytes on disk (already cleaned up)", file_id)
        raise api_error(404, "File content is no longer available", "FILE_EXPIRED")

    logger.info("File downloaded: id=%s", file_id)
    return FileResponse(
        path=file_doc.storagePath,
        filename=file_doc.originalFilename,
        media_type=file_doc.mimeType,
    )
