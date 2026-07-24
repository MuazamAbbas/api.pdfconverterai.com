"""`jobs` module generic poll endpoint (Handbook Part C.4/C.5).

Works for any job `type` - Tier 2 tool routers (currently `pdf`, later
`image`/`ai`) each create jobs via their own module's job-creation logic
(see `app/routers/pdf.py`), but this is the one place a client polls status
for any of them: `POST /pdf/convert` -> poll `GET /jobs/{id}`.
"""
import logging

from fastapi import APIRouter, Depends

from app.core.security import verify_api_key
from app.services.files.service import get_file_by_id
from app.services.jobs.service import get_job
from app.shared.responses import api_error, envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])


@router.get("/{job_id}", summary="Poll a job's status")
async def get_job_status(job_id: str, api_key: dict = Depends(verify_api_key)):
    job = await get_job(job_id)
    if job is None:
        logger.warning("Job not found: %s", job_id)
        raise api_error(404, "Job not found", "JOB_NOT_FOUND")

    # Best-effort ownership check via the job's input file. If the input
    # file has already expired/been cleaned up we can't re-verify ownership
    # from it - allow the poll through rather than permanently locking a
    # caller out of their own completed job's result.
    owner_id = str(api_key["key_data"]["_id"])
    file_doc = await get_file_by_id(str(job.fileId))
    if file_doc is not None and str(file_doc.ownerApiKeyId) != owner_id:
        logger.warning("Job %s not owned by requesting key", job_id)
        raise api_error(403, "Not authorized to view this job", "JOB_FORBIDDEN")

    data = {"job_id": str(job.id), "type": job.type, "status": job.status.value}
    if job.result is not None:
        data["result"] = job.result
    if job.error is not None:
        data["error"] = job.error

    logger.debug("Job %s status=%s", job_id, job.status.value)
    return envelope(True, "Job status retrieved", data=data)
