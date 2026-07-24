"""Shared Pydantic building blocks for MongoDB-backed schemas.

Kept separate from any single collection's schema module so `file.py` and
`job.py` (and future schema modules) can share the same ObjectId handling
and expiry default without duplicating it. See Handbook Part C.9 (ADR-007)
for the collection/field-naming conventions these schemas follow.
"""

from datetime import datetime, timedelta
from typing import Any

from bson import ObjectId
from pydantic import GetCoreSchemaHandler
from pydantic_core import core_schema

from app.core.config import settings


class PyObjectId(ObjectId):
    """A bson.ObjectId that Pydantic v2 knows how to validate/serialize.

    Values are kept as real `bson.ObjectId` instances in Python so Motor
    writes them as proper BSON ObjectIds (not strings) — this matters for
    foreign-key-style fields like `files.ownerApiKeyId` and `jobs.fileId`,
    which must be the same BSON type as the `_id` they reference for
    equality queries/joins to work. They serialize to plain strings for
    JSON API responses.
    """

    @classmethod
    def __get_pydantic_core_schema__(
        cls, source_type: Any, handler: GetCoreSchemaHandler
    ) -> core_schema.CoreSchema:
        return core_schema.union_schema(
            [
                core_schema.is_instance_schema(ObjectId),
                core_schema.chain_schema(
                    [
                        core_schema.str_schema(),
                        core_schema.no_info_plain_validator_function(cls.validate),
                    ]
                ),
            ],
            serialization=core_schema.plain_serializer_function_ser_schema(str, when_used="json"),
        )

    @classmethod
    def validate(cls, value: str) -> ObjectId:
        if not ObjectId.is_valid(value):
            raise ValueError(f"Invalid ObjectId: {value!r}")
        return ObjectId(value)


def default_expiry() -> datetime:
    """now + settings.file_retention_minutes.

    Default `expiresAt` for both `files` and `jobs` documents, backing the
    TTL indexes that keep Mongo cleanup in sync with the filesystem
    worker's temp-file cleanup window (Handbook Part C.1).
    """
    return datetime.utcnow() + timedelta(minutes=settings.file_retention_minutes)
