"""Job lifecycle helpers for the `jobs` collection (Handbook Part C.4/C.9).

Owns every read/write against `db.jobs` so callers (Tier 2 tool routers
creating a job, `app/worker.py` transitioning its status) never touch the
collection directly - keeps "one module = one responsibility" (Part C.3)
even though jobs are created from pdf/image/ai routers.
"""
import logging
from datetime import datetime
from typing import Any, Optional

from bson import ObjectId
from bson.errors import InvalidId

from app.core.database import db
from app.schemas.job import JobCreate, JobDocument, JobStatus

logger = logging.getLogger(__name__)


def _to_object_id(job_id: str) -> Optional[ObjectId]:
    try:
        return ObjectId(job_id)
    except (InvalidId, TypeError):
        return None


async def create_job(file_id: ObjectId, job_type: str) -> JobDocument:
    job_create = JobCreate(fileId=file_id, type=job_type)
    insert_result = await db.jobs.insert_one(job_create.model_dump(by_alias=True))
    doc = await db.jobs.find_one({"_id": insert_result.inserted_id})
    logger.info("Created job %s type=%s file=%s", insert_result.inserted_id, job_type, file_id)
    return JobDocument(**doc)


async def get_job(job_id: str) -> Optional[JobDocument]:
    oid = _to_object_id(job_id)
    if oid is None:
        return None
    doc = await db.jobs.find_one({"_id": oid})
    return JobDocument(**doc) if doc else None


async def _update(job_id: str, **fields: Any) -> None:
    oid = _to_object_id(job_id)
    if oid is None:
        logger.error("Refusing to update job with invalid id: %s", job_id)
        return
    fields["updatedAt"] = datetime.utcnow()
    await db.jobs.update_one({"_id": oid}, {"$set": fields})


async def mark_queued(job_id: str) -> None:
    await _update(job_id, status=JobStatus.QUEUED)


async def mark_processing(job_id: str) -> None:
    await _update(job_id, status=JobStatus.PROCESSING)


async def mark_completed(job_id: str, result: dict) -> None:
    await _update(job_id, status=JobStatus.COMPLETED, result=result, error=None)


async def mark_failed(job_id: str, error: str) -> None:
    await _update(job_id, status=JobStatus.FAILED, error=error)


async def increment_retry_count(job_id: str) -> None:
    oid = _to_object_id(job_id)
    if oid is None:
        return
    await db.jobs.update_one(
        {"_id": oid},
        {"$inc": {"retryCount": 1}, "$set": {"updatedAt": datetime.utcnow()}},
    )
