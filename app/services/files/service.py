"""Shared file-handling logic for the `files` module (Handbook Part C.3, C.9).

Owns: saving an upload to disk + a matching `files` Mongo record, fetching
that record back, and registering a worker-generated output file (e.g. a
converted .docx) the same way. `app/routers/files.py` and
`app/routers/pdf.py` both call `save_uploaded_file` so there is exactly one
upload implementation, not two (Handbook D.2 "reuse before creating").

No HTTP concerns live here on purpose (no HTTPException) - this module is
called from both route handlers and the ARQ worker process, which isn't a
request/response context at all.
"""
import hashlib
import logging
import os
import re
import uuid
from typing import Optional

from bson import ObjectId
from bson.errors import InvalidId
from fastapi import UploadFile

from app.core.config import settings
from app.core.database import db
from app.core.storage import STORAGE_PATH
from app.schemas.file import FileCreate, FileDocument

logger = logging.getLogger(__name__)

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

# Upload validation (Handbook Part C.10: MIME+extension+size+magic-bytes+
# sanitized filenames). `.pdf` is the only real caller today
# (`app/routers/pdf.py`'s `/pdf/upload`, `app/routers/files.py`'s generic
# `/files/upload`) - kept as a set (not a single hardcoded `.endswith(...)`
# check) so other modules can pass/extend their own allow-list later.
DEFAULT_ALLOWED_EXTENSIONS: set[str] = {".pdf"}

# Known file signatures ("magic bytes") for extensions in
# DEFAULT_ALLOWED_EXTENSIONS - verifies actual file content, not just the
# filename/extension, matches what it claims to be.
_MAGIC_BYTES: dict[str, bytes] = {
    ".pdf": b"%PDF-",
}

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024  # 1 MiB


class UploadValidationError(Exception):
    """Raised when an upload fails validation (size/extension/magic-bytes).

    Carries a safe, user-facing `message` plus a `status_code`/`error_code`
    so a router can turn this straight into `app.shared.responses.api_error`.
    Kept as a plain exception (not `HTTPException`) since this module has no
    HTTP concerns on purpose - it's also usable from non-request contexts.
    """

    def __init__(self, message: str, error_code: str, status_code: int = 400):
        self.message = message
        self.error_code = error_code
        self.status_code = status_code
        super().__init__(message)


def _sanitize_filename(filename: Optional[str]) -> str:
    """Strip path components and any character outside a conservative allow-list
    (Handbook Part C.10: sanitized filenames are one of the upload-validation layers).
    """
    name = os.path.basename(filename or "upload")
    name = _SAFE_FILENAME_RE.sub("_", name).strip("._") or "upload"
    return name[:200]


async def _read_and_validate_upload(file: UploadFile, safe_name: str, allowed_extensions: set[str]) -> bytes:
    """Extension-check, size-cap, and magic-byte-sniff an upload.

    Reads in chunks and aborts as soon as the configured size cap is
    exceeded, instead of buffering an unbounded/oversized body fully into
    memory first (Handbook Part C.10).
    """
    ext = os.path.splitext(safe_name)[1].lower()
    if ext not in allowed_extensions:
        raise UploadValidationError(
            f"Unsupported file type '{ext or 'unknown'}'", "FILE_INVALID_TYPE"
        )

    max_bytes = settings.max_upload_size_mb * 1024 * 1024
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise UploadValidationError(
                f"File exceeds the maximum upload size of {settings.max_upload_size_mb}MB",
                "FILE_TOO_LARGE",
                status_code=413,
            )
        chunks.append(chunk)
    content = b"".join(chunks)

    magic = _MAGIC_BYTES.get(ext)
    if magic is not None and not content.startswith(magic):
        raise UploadValidationError(
            "File content does not match its extension", "FILE_INVALID_CONTENT"
        )

    return content


async def save_uploaded_file(
    file: UploadFile,
    owner_api_key_id: ObjectId,
    allowed_extensions: set[str] = DEFAULT_ALLOWED_EXTENSIONS,
) -> FileDocument:
    """Write an UploadFile to disk, checksum it, and record it in `files`.

    Raises `UploadValidationError` if the upload fails extension/size/
    magic-byte validation - callers (routers) turn that into the standard
    error envelope.
    """
    safe_name = _sanitize_filename(file.filename)
    content = await _read_and_validate_upload(file, safe_name, allowed_extensions)
    checksum = hashlib.sha256(content).hexdigest()

    os.makedirs(STORAGE_PATH, exist_ok=True)
    stored_name = f"{uuid.uuid4()}-{safe_name}"
    storage_path = os.path.join(STORAGE_PATH, stored_name)
    with open(storage_path, "wb") as out:
        out.write(content)

    file_create = FileCreate(
        storagePath=storage_path,
        checksum=checksum,
        originalFilename=safe_name,
        sizeBytes=len(content),
        mimeType=file.content_type or "application/octet-stream",
        ownerApiKeyId=owner_api_key_id,
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    logger.info("Saved upload '%s' as file %s (%d bytes)", safe_name, insert_result.inserted_id, len(content))
    return FileDocument(**doc)


async def save_output_file(
    local_path: str, owner_api_key_id: ObjectId, original_filename: str, mime_type: str
) -> FileDocument:
    """Register a worker-generated output file (already written to disk) in `files`.

    Same retention/checksum treatment as an uploaded input file (Handbook
    design decision: output artifacts like a converted .docx get their own
    `files` document rather than being embedded in `jobs.result`).
    """
    safe_name = _sanitize_filename(original_filename)
    with open(local_path, "rb") as f:
        content = f.read()
    checksum = hashlib.sha256(content).hexdigest()

    file_create = FileCreate(
        storagePath=local_path,
        checksum=checksum,
        originalFilename=safe_name,
        sizeBytes=len(content),
        mimeType=mime_type,
        ownerApiKeyId=owner_api_key_id,
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    logger.info("Registered output file '%s' as file %s", safe_name, insert_result.inserted_id)
    return FileDocument(**doc)


async def get_file_by_id(file_id: str) -> Optional[FileDocument]:
    try:
        oid = ObjectId(file_id)
    except (InvalidId, TypeError):
        return None
    doc = await db.files.find_one({"_id": oid})
    return FileDocument(**doc) if doc else None
