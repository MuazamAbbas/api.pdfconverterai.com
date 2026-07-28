"""Retry classification contract for `pdf_summarize` (Handbook Part C.4,
ADR-003; ADR-015 Open Item 2 - model-loading migration): `app.worker._run_job`
(via the `pdf_summarize` task function) must retry a `TransientProcessingError`
up to `MAX_TRIES`, and must fail a `PermanentProcessingError` immediately with
no retry - exactly the same contract `tests/test_worker_retry.py` verifies for
`pdf_convert` and `tests/test_worker_retry_image.py` verifies for `image_ocr`,
applied to `pdf_summarize` now that its model is loaded once per worker
process (`app/worker.py`'s `on_startup`) instead of at import time.

Uses real Mongo `files`/`jobs` documents (real local Mongo, per the task
brief), and `app.worker.pdf_summarize(ctx, job_id)` is called directly with a
hand-built `ctx` dict standing in for the real ARQ worker-process context -
the same "call the task function directly" style the sibling retry test
modules use, just without spinning up a second worker process. The `bart-
large-cnn` model itself is never loaded here: `ctx["summarizer_pipeline"]` is
either omitted (to exercise the new "model not loaded yet" transient path) or
a lightweight fake callable (to exercise success/permanent-failure paths)
rather than the real Hugging Face pipeline - see
`tests/test_worker_startup_shutdown.py` for the (mocked) coverage of the real
`on_startup` model-loading hook itself.
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


async def _make_pdf_file_doc(owner_id, content: bytes, filename: str):
    from app.core.database import db
    from app.schemas.file import FileCreate, FileDocument
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
    return FileDocument(**doc)


async def test_no_pipeline_in_ctx_retries_instead_of_failing(api_key, test_pdf_bytes):
    """A `pdf_summarize` job picked up by a worker process that hasn't
    finished `on_startup()` yet (no `summarizer_pipeline` in `ctx`) must be
    treated as transient and retried, not failed outright - this is the
    behavior that's new in this migration (previously the model was loaded
    eagerly at import time, so this state could never happen)."""
    file_doc = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "summarize-no-pipeline-test.pdf")
    job = await create_job(file_doc.id, "pdf_summarize")

    with pytest.raises(Retry):
        await worker.pdf_summarize({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_permanent_failure_corrupt_pdf_fails_immediately_no_retry(
    api_key, corrupt_pdf_bytes
):
    """Regression test for the bug found during the ai-module-loading session:
    `summarize_pdf_service` used to call `pymupdf.open(file_path)` with no
    try/except around the open call - for a corrupted/malformed PDF, pymupdf
    raises `pymupdf.FileDataError` (a `RuntimeError` subclass), never
    `ValueError`, so `PdfSummarizeProcessor.execute()`'s `except ValueError`
    mapping never caught it and it fell through to the generic
    `except Exception` branch, becoming `TransientProcessingError` instead of
    `PermanentProcessingError` - i.e. a corrupted PDF got retried 3 times
    before failing, unlike `pdf_convert`'s correct immediate-permanent-failure
    behavior for the same input (see
    `tests/test_worker_retry.py::test_permanent_failure_corrupt_pdf_fails_immediately_no_retry`).
    Fixed by wrapping `pymupdf.open()` in `summarize_pdf_service` to convert
    open/parse failures into `ValueError`, mirroring
    `extract_text_from_pdf`'s convention in `convert.py`.
    """
    file_doc = await _make_pdf_file_doc(api_key["id"], corrupt_pdf_bytes, "summarize-corrupt-test.pdf")
    job = await create_job(file_doc.id, "pdf_summarize")

    async def _fake_summarizer(text, **kwargs):
        raise AssertionError("should never be reached: pymupdf can't open a corrupt PDF")

    # job_try=1: even on a fresh, first attempt, a permanent error must not retry.
    await worker.pdf_summarize({"job_try": 1, "summarizer_pipeline": _fake_summarizer}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "permanent failures must never consume/increment retries"
    assert updated.error and "stack" not in updated.error.lower()


async def test_transient_failure_exhausted_retries_marks_failed_no_more_retry(api_key, test_pdf_bytes):
    """Same "no pipeline in ctx" transient condition as above, but on the
    final allowed attempt - must fail, not retry again."""
    file_doc = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "summarize-exhausted-test.pdf")
    job = await create_job(file_doc.id, "pdf_summarize")

    await worker.pdf_summarize({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "the final exhausted attempt takes the mark-failed branch, not increment+retry"
    assert updated.error == "Processing failed after multiple attempts"


async def test_successful_summarize_with_preloaded_pipeline_completes(api_key, test_pdf_bytes):
    """Once `ctx["summarizer_pipeline"]` is present (as it would be after a
    real worker's `on_startup()` finishes), the job completes normally - a
    fake pipeline stands in for the real `bart-large-cnn` model here (see
    module docstring)."""
    file_doc = await _make_pdf_file_doc(api_key["id"], test_pdf_bytes, "summarize-success-test.pdf")
    job = await create_job(file_doc.id, "pdf_summarize")

    def _fake_pipeline(text, max_length=150, min_length=30, do_sample=False):
        return [{"summary_text": "A fake but plausible summary."}]

    await worker.pdf_summarize(
        {"job_try": 1, "summarizer_pipeline": _fake_pipeline}, str(job.id)
    )

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result["summary"] == "A fake but plausible summary."
