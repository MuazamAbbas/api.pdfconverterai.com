"""Regression coverage for `JobBase.fileIds` (Handbook Part I.2 - Merge PDF,
the first multi-file Tier 2 tool): confirms the new optional field doesn't
break parsing of pre-existing-shape `jobs` documents (created before this
field existed, with no `fileIds` key in Mongo at all - single-file
`pdf_convert`/`pdf_to_word`/`pdf_summarize` jobs), and that `JobCreate`
defaults it to `None` for ordinary single-file job creation via
`create_job`.

The three existing single-file job types' own end-to-end behavior (HTTP
round trip, worker execution, retry classification) is unmodified and
covered unchanged by `tests/test_files_jobs_pdf_flow.py`,
`tests/test_worker_retry.py`, and `tests/test_worker_retry_pdf_summarize.py`
- this module covers only the schema-level parsing contract those tests
don't directly assert on.
"""
from datetime import datetime

import pytest
from bson import ObjectId

from app.schemas.job import JobCreate, JobDocument
from app.services.jobs.service import create_job, get_job

pytestmark = pytest.mark.asyncio(loop_scope="session")


def test_job_document_parses_a_pre_existing_style_doc_with_no_fileids_key():
    """Simulates a `jobs` document exactly as it would have existed in Mongo
    before this branch - no `fileIds` key present at all, not even `null`."""
    raw_doc = {
        "_id": ObjectId(),
        "fileId": ObjectId(),
        # Deliberately no "fileIds" key here.
        "type": "pdf_convert",
        "status": "completed",
        "result": {"text": "hello"},
        "error": None,
        "retryCount": 0,
        "maxRetries": 3,
        "createdAt": datetime.utcnow(),
        "updatedAt": datetime.utcnow(),
        "expiresAt": datetime.utcnow(),
    }

    job = JobDocument(**raw_doc)
    assert job.fileIds is None
    assert job.type == "pdf_convert"
    assert job.status.value == "completed"


def test_job_create_defaults_fileids_to_none_for_ordinary_single_file_jobs():
    job_create = JobCreate(fileId=ObjectId(), type="pdf_convert")
    assert job_create.fileIds is None

    dumped = job_create.model_dump(by_alias=True)
    assert dumped["fileIds"] is None
    assert "fileId" in dumped


async def test_create_job_persists_no_fileids_side_effect_for_single_file_job_types(api_key):
    """`create_job` (used by `pdf_convert`/`pdf_to_word`/`pdf_summarize`,
    unchanged by this branch) must keep writing `fileIds: None` - not, say,
    accidentally wrapping `fileId` into a one-item list - so `GET /jobs/{id}`
    and every other single-file consumer keep seeing exactly the shape they
    did before this field was added."""
    file_id = ObjectId()
    job = await create_job(file_id, "pdf_convert", api_key["id"])
    try:
        assert job.fileIds is None
        assert job.fileId == file_id

        reloaded = await get_job(str(job.id))
        assert reloaded.fileIds is None
        assert reloaded.fileId == file_id
    finally:
        from app.core.database import db

        await db.jobs.delete_one({"_id": job.id})
