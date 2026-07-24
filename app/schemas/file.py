"""Pydantic models for the `files` collection.

MongoDB stores metadata about an uploaded/generated file only — never the
file bytes themselves (ADR-007, Handbook Part C.9). The actual content
lives on local VPS disk at `storagePath`; this collection is how the API
and worker find it, verify it (`checksum`), and know when to stop
trusting it (`expiresAt`, enforced by a TTL index).

Collection: `files` (lowercase-plural). Fields: camelCase. Every document
gets `createdAt` (and, since files don't change after upload, no
`updatedAt` is tracked here — see note on JobBase for jobs, which do
change over their lifecycle).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PyObjectId, default_expiry


class FileBase(BaseModel):
    """Fields shared between creating and reading a `files` document."""

    storagePath: str = Field(
        ...,
        description="Absolute path to the file on local VPS storage (see infra-agent storage layout). Never the file content itself.",
    )
    checksum: str = Field(
        ..., description="sha256 hex digest of the file contents, for integrity verification"
    )
    originalFilename: str = Field(..., description="User-supplied filename, sanitized before storage")
    sizeBytes: int = Field(..., ge=0)
    mimeType: str
    ownerApiKeyId: PyObjectId = Field(
        ..., description="References api_keys._id — which API key uploaded/owns this file"
    )


class FileCreate(FileBase):
    """Shape used when inserting a new `files` document.

    `createdAt`/`expiresAt` default at construction time so callers only
    need to supply the file-specific fields; pass explicit values to
    override (e.g. a longer expiry for a specific job type), but keep any
    override within the Handbook C.1 30-60 min retention window unless a
    documented exception applies.
    """

    createdAt: datetime = Field(default_factory=datetime.utcnow)
    expiresAt: datetime = Field(default_factory=default_expiry)


class FileDocument(FileBase):
    """Shape of a `files` document as read back from MongoDB."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    createdAt: datetime
    expiresAt: datetime
