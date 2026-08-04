"""Pydantic models for the `jobs` collection.

Every processing request creates a Job (ADR-003, Handbook Part C.4):
Pending -> Queued -> Processing -> (Completed | Failed | Expired). This is
"the backend's heartbeat" for Tier 2 (queued/processing) tools — never used
for Tier 1 (instant, synchronous) tools, which don't touch this collection
at all.

Collection: `jobs` (lowercase-plural). Fields: camelCase. Every document
gets `createdAt`/`updatedAt`.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PyObjectId, default_expiry


class JobStatus(str, Enum):
    """Job lifecycle states (Handbook Part C.4 / ADR-003)."""

    PENDING = "pending"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"


class JobBase(BaseModel):
    fileId: PyObjectId = Field(
        ..., description="References files._id — the input file this job processes"
    )
    fileIds: Optional[list[PyObjectId]] = Field(
        default=None,
        description=(
            "References files._id for every input file, for multi-file job "
            "types (e.g. pdf_merge). None for single-file job types, which "
            "continue to use `fileId` only - `fileId` is still set (to the "
            "first entry) for multi-file jobs too, so existing single-file "
            "ownership-check code (e.g. GET /jobs/{id}) keeps working "
            "unchanged."
        ),
    )
    # Intentionally a free-form string, not a fixed Enum: the job "type"
    # doubles as the tool identifier, and Tier 2 tools span multiple
    # modules (pdf, image, ai, ...) per Handbook Part I.2. A hardcoded enum
    # here would force every module to edit this shared schema file to add
    # a job type, which cuts against "one module = one responsibility."
    # Recommended convention: "{module}_{action}", e.g. "pdf_convert",
    # "pdf_to_word", "pdf_summarize", "image_ocr", "ai_caption".
    type: str = Field(
        ...,
        description='Job/tool type, e.g. "pdf_convert", "pdf_to_word", "pdf_summarize"',
    )
    status: JobStatus = Field(default=JobStatus.PENDING)
    result: Optional[dict[str, Any]] = Field(
        default=None,
        description=(
            "Job-type-specific result payload; shape depends on `type` "
            "(caller/worker's contract, not enforced here). Must never "
            "contain raw file bytes — large outputs belong on disk as "
            "their own `files` document, referenced here (e.g. by an "
            "`outputFileId` key) rather than embedded."
        ),
    )
    error: Optional[str] = Field(
        default=None,
        description=(
            "Safe, generic error message only (e.g. 'Unsupported file "
            "format') — never a stack trace or internal exception detail "
            "(Handbook Part C.10)."
        ),
    )
    retryCount: int = Field(default=0, ge=0)
    # ADR-003: retries are selective — temporary faults (e.g. transient IO/
    # worker crash) are retried up to maxRetries; corrupted files or
    # unsupported formats should be marked Failed immediately by the
    # worker without incrementing/consuming retries.
    maxRetries: int = Field(default=3, ge=0)
    # ADR-016: stamped directly at job-creation time (mirrors
    # files.ownerApiKeyId, see FileBase) so GET /jobs/{id}'s ownership
    # check no longer has to derive ownership transitively via the
    # linked File record, which fails open if that File has already
    # TTL-expired (files.py TTL-cleans upload records well before a job's
    # own `expiresAt`). Optional — NOT required — because job documents
    # created before this fix went live won't have it; the ownership-check
    # fallback logic (app/routers/jobs.py) treats `None` here as "legacy
    # doc, fall back to the file-based check" and a set-but-mismatched
    # value as a hard 403. No backfill/migration was needed for those
    # legacy docs: `jobs.expiresAt` uses the same short
    # `settings.file_retention_minutes` (60 min) window as `files`, and
    # `jobs_expiresAt_ttl` (app/core/database.py) already TTL-cleans them,
    # so every unstamped job self-expires shortly after this deploy.
    ownerApiKeyId: Optional[PyObjectId] = Field(
        default=None,
        description=(
            "References api_keys._id — which API key created this job. "
            "None only for legacy pre-ADR-016 documents; every job created "
            "via app/services/jobs/service.py now always sets this."
        ),
    )


class JobCreate(JobBase):
    """Shape used when inserting a new `jobs` document.

    `createdAt`/`updatedAt`/`expiresAt` default at construction time.
    `expiresAt` reuses the same retention window as `files` so a job's
    metadata doesn't outlive the input/output files it refers to.
    """

    createdAt: datetime = Field(default_factory=datetime.utcnow)
    updatedAt: datetime = Field(default_factory=datetime.utcnow)
    expiresAt: datetime = Field(default_factory=default_expiry)


class JobDocument(JobBase):
    """Shape of a `jobs` document as read back from MongoDB."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    createdAt: datetime
    updatedAt: datetime
    expiresAt: datetime
