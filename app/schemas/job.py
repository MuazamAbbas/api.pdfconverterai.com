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
