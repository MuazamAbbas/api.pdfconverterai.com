"""Retry classification contract for a concrete Tier 2 processor
(Handbook Part C.4, ADR-003): `app.worker._run_job` (via the `pdf_convert`
task function) must retry a `TransientProcessingError` up to `MAX_TRIES`,
and must fail a `PermanentProcessingError` immediately with no retry.

Uses `app.services.pdf.processors.PdfConvertProcessor` against real files/
jobs Mongo documents (real local Mongo, per the task brief) - a corrupt PDF
fixture drives the permanent-failure path; the transient path is forced by
monkeypatching the text-extraction call the processor wraps, since a real
transient I/O fault isn't practical to reproduce on demand.

The worker's ARQ task functions are called directly (not through a live
`arq` worker process/subprocess) - this exercises the exact same
`app.worker._run_job` orchestration code the real worker calls, just without
spinning up a second process for the test suite.
"""
import os

import pytest
from arq import Retry

import app.worker as worker
from app.core.storage import STORAGE_PATH
from app.schemas.job import JobStatus
from app.services.files.service import save_output_file  # noqa: F401  (keeps parity with worker's imports)
from app.services.jobs.service import create_job, get_job

# `app.core.database.db` is a Motor client created once at import time and
# must stay bound to a single event loop for the whole session (see
# `tests/conftest.py`'s `asyncio_default_fixture_loop_scope = "session"` ini
# setting) - pin these tests to that same session-scoped loop rather than
# pytest-asyncio's per-test default, or every test after the first raises
# "Task ... attached to a different loop".
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_file_doc(owner_id, content: bytes, filename: str):
    from app.core.database import db
    from app.schemas.file import FileCreate
    import hashlib

    os.makedirs(STORAGE_PATH, exist_ok=True)
    storage_path = os.path.join(STORAGE_PATH, filename)
    with open(storage_path, "wb") as f:
        f.write(content)

    file_create = FileCreate(
        storagePath=storage_path,
        checksum=hashlib.sha256(content).hexdigest(),
        originalFilename=filename,
        sizeBytes=len(content),
        mimeType="application/pdf",
        ownerApiKeyId=owner_id,
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    from app.schemas.file import FileDocument

    return FileDocument(**doc)


async def test_permanent_failure_corrupt_pdf_fails_immediately_no_retry(api_key, corrupt_pdf_bytes):
    file_doc = await _make_file_doc(api_key["id"], corrupt_pdf_bytes, "corrupt-retry-test.pdf")
    job = await create_job(file_doc.id, "pdf_convert")

    # job_try=1: even on a fresh, first attempt, a permanent error must not retry.
    await worker.pdf_convert({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "permanent failures must never consume/increment retries"
    assert updated.error and "stack" not in updated.error.lower()


async def test_transient_failure_retries_below_max_tries(api_key, test_pdf_bytes, monkeypatch):
    file_doc = await _make_file_doc(api_key["id"], test_pdf_bytes, "transient-retry-test.pdf")
    job = await create_job(file_doc.id, "pdf_convert")

    async def _boom(path):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.pdf.processors.extract_text_from_pdf", _boom)

    with pytest.raises(Retry):
        await worker.pdf_convert({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    # Still mid-retry: not marked failed/completed, and the attempt was counted.
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_transient_failure_exhausted_retries_marks_failed_no_more_retry(
    api_key, test_pdf_bytes, monkeypatch
):
    file_doc = await _make_file_doc(api_key["id"], test_pdf_bytes, "transient-exhausted-test.pdf")
    job = await create_job(file_doc.id, "pdf_convert")

    async def _boom(path):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.pdf.processors.extract_text_from_pdf", _boom)

    # job_try == MAX_TRIES: this is the last allowed attempt - must fail, not retry again.
    await worker.pdf_convert({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "the final exhausted attempt takes the mark-failed branch, not increment+retry"
    assert updated.error == "Processing failed after multiple attempts"


async def test_successful_conversion_after_transient_retry_completes(api_key, test_pdf_bytes, monkeypatch):
    """Sanity check that the retry path isn't a dead end: once the transient
    condition clears, the next attempt (a fresh `_run_job` call, as the real
    `arq` worker would do after `Retry`) completes normally."""
    file_doc = await _make_file_doc(api_key["id"], test_pdf_bytes, "transient-recovers-test.pdf")
    job = await create_job(file_doc.id, "pdf_convert")

    calls = {"n": 0}
    from app.services.pdf.convert import extract_text_from_pdf as real_extract

    async def _flaky_once(path):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated transient disk hiccup")
        return await real_extract(path)

    monkeypatch.setattr("app.services.pdf.processors.extract_text_from_pdf", _flaky_once)

    with pytest.raises(Retry):
        await worker.pdf_convert({"job_try": 1}, str(job.id))

    # Second attempt, as arq would perform after the Retry.
    await worker.pdf_convert({"job_try": 2}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert "test PDF" in updated.result["text"]
