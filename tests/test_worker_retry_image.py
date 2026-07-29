"""Retry classification contract for `image_ocr` (Handbook Part C.4,
ADR-003): `app.worker._run_job` (via the `image_ocr` task function) must
retry a `TransientProcessingError` up to `MAX_TRIES`, and must fail a
`PermanentProcessingError` immediately with no retry - exactly the same
contract `tests/test_worker_retry.py` verifies for `pdf_convert`, applied to
the `image` module's own Tier 2 job now that it's wired onto the same Job
System (`app/routers/image.py`'s reconciliation).

Uses `app.services.image.processors.ImageOcrProcessor` against real
files/jobs Mongo documents (real local Mongo, per the task brief) -
`extract_text_from_image` is monkeypatched at its
`app.services.image.processors` import site to force each path on demand,
same as `tests/test_worker_retry.py` does for `extract_text_from_pdf` (a
real transient I/O fault isn't practical to reproduce on demand, and a real
Tesseract binary isn't reachable on this checkout at all - see
`tests/test_files_jobs_image_flow.py::_tesseract_available`'s docstring).

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
from app.services.jobs.service import create_job, get_job

# `app.core.database.db` is a Motor client created once at import time and
# must stay bound to a single event loop for the whole session (see
# `tests/conftest.py`'s `asyncio_default_fixture_loop_scope = "session"` ini
# setting) - pin these tests to that same session-scoped loop rather than
# pytest-asyncio's per-test default, or every test after the first raises
# "Task ... attached to a different loop".
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_file_doc(owner_id, content: bytes, filename: str):
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
        mimeType="image/png",
        ownerApiKeyId=owner_id,
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    return FileDocument(**doc)


async def test_permanent_failure_no_text_detected_fails_immediately_no_retry(
    api_key, blank_image_bytes, monkeypatch
):
    file_doc = await _make_file_doc(api_key["id"], blank_image_bytes, "blank-retry-test.png")
    job = await create_job(file_doc.id, "image_ocr")

    async def _no_text(image_data: bytes) -> str:
        raise ValueError("No text detected in image")

    monkeypatch.setattr("app.services.image.processors.extract_text_from_image", _no_text)

    # job_try=1: even on a fresh, first attempt, a permanent error must not retry.
    await worker.image_ocr({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "permanent failures must never consume/increment retries"
    assert updated.error and "stack" not in updated.error.lower()


async def test_transient_failure_retries_below_max_tries(api_key, image_with_text_bytes, monkeypatch):
    file_doc = await _make_file_doc(api_key["id"], image_with_text_bytes, "transient-retry-test.png")
    job = await create_job(file_doc.id, "image_ocr")

    async def _boom(image_data: bytes) -> str:
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.image.processors.extract_text_from_image", _boom)

    with pytest.raises(Retry):
        await worker.image_ocr({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    # Still mid-retry: not marked failed/completed, and the attempt was counted.
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_transient_failure_exhausted_retries_marks_failed_no_more_retry(
    api_key, image_with_text_bytes, monkeypatch
):
    file_doc = await _make_file_doc(
        api_key["id"], image_with_text_bytes, "transient-exhausted-test.png"
    )
    job = await create_job(file_doc.id, "image_ocr")

    async def _boom(image_data: bytes) -> str:
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.image.processors.extract_text_from_image", _boom)

    # job_try == MAX_TRIES: this is the last allowed attempt - must fail, not retry again.
    await worker.image_ocr({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "the final exhausted attempt takes the mark-failed branch, not increment+retry"
    assert updated.error == "Processing failed after multiple attempts"


async def test_successful_ocr_after_transient_retry_completes(api_key, image_with_text_bytes, monkeypatch):
    """Sanity check that the retry path isn't a dead end: once the transient
    condition clears, the next attempt (a fresh `_run_job` call, as the real
    `arq` worker would do after `Retry`) completes normally."""
    file_doc = await _make_file_doc(
        api_key["id"], image_with_text_bytes, "transient-recovers-test.png"
    )
    job = await create_job(file_doc.id, "image_ocr")

    calls = {"n": 0}

    async def _flaky_once(image_data: bytes) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated transient disk hiccup")
        return "HELLO WORLD"

    monkeypatch.setattr("app.services.image.processors.extract_text_from_image", _flaky_once)

    with pytest.raises(Retry):
        await worker.image_ocr({"job_try": 1}, str(job.id))

    # Second attempt, as arq would perform after the Retry.
    await worker.image_ocr({"job_try": 2}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result["text"] == "HELLO WORLD"
