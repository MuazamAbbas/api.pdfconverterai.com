"""Retry classification + orchestration contract for `pdf_split`/
`app.worker._run_job` (Handbook Part C.4, ADR-003).

Same "call the task function directly against real local Mongo/Redis-backed
Job/File documents" style as `tests/test_worker_retry.py`/
`tests/test_worker_retry_pdf_merge.py` - no separate `arq` worker process,
but the exact same orchestration code path a real worker calls.

Jobs are created via `app.services.jobs.service.create_job` directly
(bypassing `POST /pdf/split`'s own validation, which is covered separately
in `tests/test_files_jobs_pdf_split_flow.py`) so these tests focus purely on
`_run_job`'s status-transition/retry contract as exercised by `SplitProcessor`.
"""
import os
import zipfile
from io import BytesIO

import PyPDF2
import pytest
from arq import Retry

import app.worker as worker
from app.core.storage import STORAGE_PATH
from app.schemas.job import JobStatus
from app.services.files.service import get_file_by_id
from app.services.jobs.service import create_job, get_job

# Same session-scoped loop as every other test module touching Mongo (see
# tests/test_worker_retry.py's comment).
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _multi_page_pdf_bytes(num_pages: int) -> bytes:
    writer = PyPDF2.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


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


async def test_happy_path_completes_with_resolvable_output_zip(api_key):
    file_doc = await _make_pdf_file_doc(api_key["id"], _multi_page_pdf_bytes(5), "split-happy.pdf")
    job = await create_job(file_doc.id, "pdf_split", api_key["id"], params={"ranges": "1-2,4"})

    await worker.pdf_split({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result and updated.result.get("outputFileId")

    output_doc = await get_file_by_id(updated.result["outputFileId"])
    assert output_doc is not None
    assert output_doc.mimeType == "application/zip"
    assert output_doc.originalFilename == "split-happy-split.zip"
    assert os.path.exists(output_doc.storagePath)

    with zipfile.ZipFile(output_doc.storagePath) as zf:
        names = sorted(zf.namelist())
        assert names == ["split-1.pdf", "split-2.pdf"]
        # split-1.pdf == pages 1-2 (2 pages); split-2.pdf == page 4 (1 page).
        with zf.open("split-1.pdf") as f:
            assert len(PyPDF2.PdfReader(BytesIO(f.read())).pages) == 2
        with zf.open("split-2.pdf") as f:
            assert len(PyPDF2.PdfReader(BytesIO(f.read())).pages) == 1

    os.remove(output_doc.storagePath)


async def test_single_page_pdf_with_full_range_produces_one_page_zip_entry(api_key, test_pdf_bytes):
    """Edge case called out in the spec: a single-page PDF with `"1-1"` is
    valid and should produce a zip containing a single 1-page PDF."""
    file_doc = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "split-single-page.pdf")
    job = await create_job(file_doc.id, "pdf_split", api_key["id"], params={"ranges": "1-1"})

    await worker.pdf_split({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    output_doc = await get_file_by_id(updated.result["outputFileId"])
    assert output_doc is not None

    with zipfile.ZipFile(output_doc.storagePath) as zf:
        assert zf.namelist() == ["split-1.pdf"]
        with zf.open("split-1.pdf") as f:
            assert len(PyPDF2.PdfReader(BytesIO(f.read())).pages) == 1

    os.remove(output_doc.storagePath)


async def test_missing_input_file_fails_immediately_no_retry(api_key):
    from bson import ObjectId

    job = await create_job(
        ObjectId("0123456789ab0123456789ab"), "pdf_split", api_key["id"], params={"ranges": "1-1"}
    )

    await worker.pdf_split({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.error == "Input file not found or has expired"
    assert updated.retryCount == 0, "a missing input file must not be retried"


async def test_permanent_failure_corrupt_pdf_fails_immediately_no_retry(api_key, corrupt_pdf_bytes):
    file_doc = await _make_pdf_file_doc(api_key["id"], corrupt_pdf_bytes, "split-corrupt.pdf")
    job = await create_job(file_doc.id, "pdf_split", api_key["id"], params={"ranges": "1-1"})

    # job_try=1: even on a fresh, first attempt, a permanent error must not retry.
    await worker.pdf_split({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "permanent failures must never consume/increment retries"
    assert updated.error == "Invalid or unreadable PDF file"
    assert "startxref" not in updated.error and "Traceback" not in updated.error


async def test_permanent_failure_out_of_bounds_range_fails_immediately_no_retry(
    api_key, test_pdf_bytes
):
    file_doc = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "split-oob.pdf")
    job = await create_job(file_doc.id, "pdf_split", api_key["id"], params={"ranges": "1-5"})

    await worker.pdf_split({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Page range exceeds the document's page count (1)"


async def test_permanent_failure_malformed_ranges_fails_immediately_no_retry(
    api_key, test_pdf_bytes
):
    file_doc = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "split-malformed.pdf")
    job = await create_job(file_doc.id, "pdf_split", api_key["id"], params={"ranges": "abc"})

    await worker.pdf_split({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Invalid page range syntax"


async def test_transient_failure_retries_below_max_tries(api_key, monkeypatch):
    file_doc = await _make_pdf_file_doc(api_key["id"], _multi_page_pdf_bytes(3), "split-transient.pdf")
    job = await create_job(file_doc.id, "pdf_split", api_key["id"], params={"ranges": "1-2"})

    def _boom(self, page, **kwargs):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr(PyPDF2.PdfWriter, "add_page", _boom)

    with pytest.raises(Retry):
        await worker.pdf_split({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    # Still mid-retry: not marked failed/completed, and the attempt was counted.
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_transient_failure_exhausted_retries_marks_failed_no_more_retry(api_key, monkeypatch):
    file_doc = await _make_pdf_file_doc(
        api_key["id"], _multi_page_pdf_bytes(3), "split-exhausted.pdf"
    )
    job = await create_job(file_doc.id, "pdf_split", api_key["id"], params={"ranges": "1-2"})

    def _boom(self, page, **kwargs):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr(PyPDF2.PdfWriter, "add_page", _boom)

    # job_try == MAX_TRIES: this is the last allowed attempt - must fail, not retry again.
    await worker.pdf_split({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, (
        "the final exhausted attempt takes the mark-failed branch, not increment+retry"
    )
    assert updated.error == "Processing failed after multiple attempts"


async def test_successful_split_after_transient_retry_completes(api_key, monkeypatch):
    """Sanity check that the retry path isn't a dead end: once the transient
    condition clears, the next attempt (a fresh `_run_job` call, as the real
    `arq` worker would do after `Retry`) completes normally."""
    file_doc = await _make_pdf_file_doc(
        api_key["id"], _multi_page_pdf_bytes(3), "split-recovers.pdf"
    )
    job = await create_job(file_doc.id, "pdf_split", api_key["id"], params={"ranges": "1-2"})

    calls = {"n": 0}
    real_add_page = PyPDF2.PdfWriter.add_page

    def _flaky_once(self, page, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated transient disk hiccup")
        return real_add_page(self, page, **kwargs)

    monkeypatch.setattr(PyPDF2.PdfWriter, "add_page", _flaky_once)

    with pytest.raises(Retry):
        await worker.pdf_split({"job_try": 1}, str(job.id))

    # Second attempt, as arq would perform after the Retry.
    await worker.pdf_split({"job_try": 2}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    output_doc = await get_file_by_id(updated.result["outputFileId"])
    assert output_doc is not None
    os.remove(output_doc.storagePath)
