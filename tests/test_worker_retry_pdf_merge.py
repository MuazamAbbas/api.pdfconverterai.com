"""Retry classification + orchestration contract for `pdf_merge`/
`app.worker._run_multi_file_job` (Handbook Part C.4, ADR-003; Handbook Part
I.2 - the first multi-file Tier 2 tool).

Same "call the task function directly against real local Mongo/Redis-backed
Job/File documents" style as `tests/test_worker_retry.py`/
`tests/test_worker_retry_pdf_summarize.py` - no separate `arq` worker
process, but the exact same orchestration code path a real worker calls.

Jobs are created via `app.services.jobs.service.create_multi_file_job`
directly (bypassing `POST /pdf/merge`'s own validation, which is covered
separately in `tests/test_files_jobs_pdf_merge_flow.py`) so these tests focus
purely on `_run_multi_file_job`'s status-transition/retry contract.
"""
import os

import PyPDF2
import pytest
from arq import Retry

import app.worker as worker
from app.core.storage import STORAGE_PATH
from app.schemas.job import JobStatus
from app.services.files.service import get_file_by_id
from app.services.jobs.service import create_multi_file_job, get_job

# Same session-scoped loop as every other test module touching Mongo (see
# tests/test_worker_retry.py's comment).
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_pdf_file_doc(owner_id, content: bytes, filename: str):
    import hashlib

    from app.core.database import db
    from app.schemas.file import FileCreate, FileDocument

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
    return FileDocument(**doc)


async def test_happy_path_completes_with_resolvable_output_file(api_key, test_pdf_bytes):
    file_a = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-happy-a.pdf")
    file_b = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-happy-b.pdf")
    job = await create_multi_file_job([file_a.id, file_b.id], "pdf_merge")

    await worker.pdf_merge({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result and updated.result.get("outputFileId")

    output_doc = await get_file_by_id(updated.result["outputFileId"])
    assert output_doc is not None
    assert output_doc.mimeType == "application/pdf"
    assert output_doc.originalFilename == "merged.pdf"
    assert os.path.exists(output_doc.storagePath)

    reader = PyPDF2.PdfReader(output_doc.storagePath)
    assert len(reader.pages) == 2  # one page from each of the two 1-page inputs

    os.remove(output_doc.storagePath)


async def test_missing_input_file_fails_immediately_no_retry(api_key, test_pdf_bytes):
    file_a = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-missing-a.pdf")
    job = await create_multi_file_job(
        [file_a.id, "0123456789ab0123456789ab"], "pdf_merge"
    )

    await worker.pdf_merge({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.error == "Input file not found or has expired"
    assert updated.retryCount == 0, "a missing input file must not be retried"


async def test_permanent_failure_corrupt_pdf_among_valid_fails_immediately_no_retry(
    api_key, test_pdf_bytes, corrupt_pdf_bytes
):
    file_a = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-corrupt-valid.pdf")
    file_b = await _make_pdf_file_doc(api_key["id"], corrupt_pdf_bytes, "merge-corrupt-bad.pdf")
    job = await create_multi_file_job([file_a.id, file_b.id], "pdf_merge")

    # job_try=1: even on a fresh, first attempt, a permanent error must not retry.
    await worker.pdf_merge({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "permanent failures must never consume/increment retries"
    assert updated.error == "One or more input PDF files are invalid or unreadable"
    assert "startxref" not in updated.error and "Traceback" not in updated.error


async def test_transient_failure_retries_below_max_tries(api_key, test_pdf_bytes, monkeypatch):
    file_a = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-transient-a.pdf")
    file_b = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-transient-b.pdf")
    job = await create_multi_file_job([file_a.id, file_b.id], "pdf_merge")

    def _boom(self, path, **kwargs):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr(PyPDF2.PdfMerger, "append", _boom)

    with pytest.raises(Retry):
        await worker.pdf_merge({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    # Still mid-retry: not marked failed/completed, and the attempt was counted.
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_transient_failure_exhausted_retries_marks_failed_no_more_retry(
    api_key, test_pdf_bytes, monkeypatch
):
    file_a = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-exhausted-a.pdf")
    file_b = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-exhausted-b.pdf")
    job = await create_multi_file_job([file_a.id, file_b.id], "pdf_merge")

    def _boom(self, path, **kwargs):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr(PyPDF2.PdfMerger, "append", _boom)

    # job_try == MAX_TRIES: this is the last allowed attempt - must fail, not retry again.
    await worker.pdf_merge({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, (
        "the final exhausted attempt takes the mark-failed branch, not increment+retry"
    )
    assert updated.error == "Processing failed after multiple attempts"


async def test_successful_merge_after_transient_retry_completes(
    api_key, test_pdf_bytes, monkeypatch
):
    """Sanity check that the retry path isn't a dead end: once the transient
    condition clears, the next attempt (a fresh `_run_multi_file_job` call, as
    the real `arq` worker would do after `Retry`) completes normally."""
    file_a = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-recovers-a.pdf")
    file_b = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "merge-recovers-b.pdf")
    job = await create_multi_file_job([file_a.id, file_b.id], "pdf_merge")

    calls = {"n": 0}
    real_append = PyPDF2.PdfMerger.append

    def _flaky_once(self, path, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated transient disk hiccup")
        return real_append(self, path, **kwargs)

    monkeypatch.setattr(PyPDF2.PdfMerger, "append", _flaky_once)

    with pytest.raises(Retry):
        await worker.pdf_merge({"job_try": 1}, str(job.id))

    # Second attempt, as arq would perform after the Retry.
    await worker.pdf_merge({"job_try": 2}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    output_doc = await get_file_by_id(updated.result["outputFileId"])
    assert output_doc is not None
    os.remove(output_doc.storagePath)
