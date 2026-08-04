"""Retry classification contract for `text_paraphrase`/`text_summarize`/
`web_tools_summarize` (Handbook Part C.4, ADR-003; text/web_tools Job System
wiring), mirroring `tests/test_worker_retry_pdf_summarize.py` /
`tests/test_worker_retry_image.py`: `app.worker._run_job` (via each task
function) must retry a `TransientProcessingError` up to `MAX_TRIES`, and must
fail a `PermanentProcessingError` immediately with no retry.

Uses real Mongo `files`/`jobs` documents (real local Mongo, per the task
brief), and `app.worker.text_paraphrase|text_summarize|web_tools_summarize(ctx,
job_id)` are called directly with a hand-built `ctx` dict standing in for the
real ARQ worker-process context - the same "call the task function directly"
style the sibling retry test modules use. Neither the real t5-small models nor
a real network fetch are ever exercised here: `ctx["paraphrase_pipeline"]`/
`ctx["summarize_pipeline"]` are either omitted (to exercise the "model not
loaded yet" transient path) or a lightweight fake callable (to exercise
success paths), and `web_tools_summarize`'s page fetch is monkeypatched at
`app.services.web_tools.summarize.fetch_webpage_text`'s definition site
(never real `aiohttp`), matching `tests/test_web_tools_processor.py`'s idiom.
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


async def _make_text_file_doc(owner_id, content: str, filename: str):
    import hashlib

    from app.core.database import db
    from app.schemas.file import FileCreate, FileDocument

    os.makedirs(STORAGE_PATH, exist_ok=True)
    storage_path = os.path.join(STORAGE_PATH, filename)
    with open(storage_path, "w", encoding="utf-8") as f:
        f.write(content)
    encoded = content.encode("utf-8")

    file_create = FileCreate(
        storagePath=storage_path,
        checksum=hashlib.sha256(encoded).hexdigest(),
        originalFilename=filename,
        sizeBytes=len(encoded),
        mimeType="text/plain",
        ownerApiKeyId=owner_id,
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    return FileDocument(**doc)


_LONG_ENOUGH_TEXT = (
    "This is a sufficiently long piece of source text so that the "
    "text_summarize job's own >=50 character validation rule passes cleanly."
)
assert len(_LONG_ENOUGH_TEXT) >= 50


# --- text_paraphrase ----------------------------------------------------------


async def test_paraphrase_no_pipeline_in_ctx_retries_instead_of_failing(api_key):
    file_doc = await _make_text_file_doc(api_key["id"], "Some text to paraphrase.", "paraphrase-no-pipeline.txt")
    job = await create_job(file_doc.id, "text_paraphrase", api_key["id"])

    with pytest.raises(Retry):
        await worker.text_paraphrase({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_paraphrase_permanent_failure_empty_text_fails_immediately_no_retry(api_key):
    """An empty source text fails `TextParaphraseProcessor.validate()` before
    `execute()` ever runs - a permanent, non-retryable failure regardless of
    whether the pipeline is loaded."""
    file_doc = await _make_text_file_doc(api_key["id"], "   ", "paraphrase-empty.txt")
    job = await create_job(file_doc.id, "text_paraphrase", api_key["id"])

    await worker.text_paraphrase({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error and "Traceback" not in updated.error


async def test_paraphrase_transient_failure_exhausted_retries_marks_failed_no_more_retry(api_key):
    file_doc = await _make_text_file_doc(api_key["id"], "Some text to paraphrase.", "paraphrase-exhausted.txt")
    job = await create_job(file_doc.id, "text_paraphrase", api_key["id"])

    await worker.text_paraphrase({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Processing failed after multiple attempts"


async def test_paraphrase_successful_with_preloaded_pipeline_completes(api_key):
    file_doc = await _make_text_file_doc(api_key["id"], "Some text to paraphrase.", "paraphrase-success.txt")
    job = await create_job(file_doc.id, "text_paraphrase", api_key["id"])

    def _fake_pipeline(input_text, **kwargs):
        return [{"generated_text": "A fake but plausible paraphrase."}]

    await worker.text_paraphrase({"job_try": 1, "paraphrase_pipeline": _fake_pipeline}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result["paraphrased"] == "A fake but plausible paraphrase."


# --- text_summarize -----------------------------------------------------------


async def test_summarize_no_pipeline_in_ctx_retries_instead_of_failing(api_key):
    file_doc = await _make_text_file_doc(api_key["id"], _LONG_ENOUGH_TEXT, "summarize-no-pipeline.txt")
    job = await create_job(file_doc.id, "text_summarize", api_key["id"])

    with pytest.raises(Retry):
        await worker.text_summarize({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_summarize_permanent_failure_too_short_text_fails_immediately_no_retry(api_key):
    file_doc = await _make_text_file_doc(api_key["id"], "too short", "summarize-short.txt")
    job = await create_job(file_doc.id, "text_summarize", api_key["id"])

    await worker.text_summarize({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error and "Traceback" not in updated.error


async def test_summarize_transient_failure_exhausted_retries_marks_failed_no_more_retry(api_key):
    file_doc = await _make_text_file_doc(api_key["id"], _LONG_ENOUGH_TEXT, "summarize-exhausted.txt")
    job = await create_job(file_doc.id, "text_summarize", api_key["id"])

    await worker.text_summarize({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Processing failed after multiple attempts"


async def test_summarize_successful_with_preloaded_pipeline_completes(api_key):
    file_doc = await _make_text_file_doc(api_key["id"], _LONG_ENOUGH_TEXT, "summarize-success.txt")
    job = await create_job(file_doc.id, "text_summarize", api_key["id"])

    def _fake_pipeline(text, max_length=50, min_length=10, do_sample=False, num_beams=4, length_penalty=1.0):
        return [{"summary_text": "A fake but plausible summary."}]

    await worker.text_summarize({"job_try": 1, "summarize_pipeline": _fake_pipeline}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result["summary"] == "A fake but plausible summary."


# --- web_tools_summarize -------------------------------------------------------


async def test_web_tools_permanent_failure_bad_url_fails_immediately_no_retry(api_key):
    """`WebToolsSummarizeProcessor.validate()` rejects a non-http(s) URL
    before `execute()` (and therefore any network fetch) ever runs."""
    file_doc = await _make_text_file_doc(api_key["id"], "ftp://example.com", "web-tools-bad-url.txt")
    job = await create_job(file_doc.id, "web_tools_summarize", api_key["id"])

    await worker.web_tools_summarize({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error and "Traceback" not in updated.error


async def test_web_tools_no_pipeline_in_ctx_retries_instead_of_failing(api_key, monkeypatch):
    file_doc = await _make_text_file_doc(api_key["id"], "https://example.com/article", "web-tools-no-pipeline.txt")
    job = await create_job(file_doc.id, "web_tools_summarize", api_key["id"])

    async def _fake_fetch(url):
        return "Fetched webpage text, long enough to be summarized." * 2

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)

    with pytest.raises(Retry):
        await worker.web_tools_summarize({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_web_tools_network_error_fetching_page_retries(api_key, monkeypatch):
    file_doc = await _make_text_file_doc(api_key["id"], "https://example.com/article", "web-tools-network-error.txt")
    job = await create_job(file_doc.id, "web_tools_summarize", api_key["id"])

    import aiohttp

    async def _fake_fetch(url):
        raise aiohttp.ClientConnectionError("simulated connection failure")

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)

    with pytest.raises(Retry):
        await worker.web_tools_summarize({"job_try": 1, "summarize_pipeline": object()}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_web_tools_transient_failure_exhausted_retries_marks_failed_no_more_retry(api_key, monkeypatch):
    file_doc = await _make_text_file_doc(api_key["id"], "https://example.com/article", "web-tools-exhausted.txt")
    job = await create_job(file_doc.id, "web_tools_summarize", api_key["id"])

    async def _fake_fetch(url):
        return "Fetched webpage text, long enough to be summarized." * 2

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)

    await worker.web_tools_summarize({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Processing failed after multiple attempts"


async def test_web_tools_successful_with_preloaded_pipeline_completes(api_key, monkeypatch):
    file_doc = await _make_text_file_doc(api_key["id"], "https://example.com/article", "web-tools-success.txt")
    job = await create_job(file_doc.id, "web_tools_summarize", api_key["id"])

    async def _fake_fetch(url):
        return "Fetched webpage paragraph text, long enough to summarize."

    def _fake_pipeline(text, max_length=150, min_length=30, do_sample=False):
        return [{"summary_text": "A fake but plausible webpage summary."}]

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)

    await worker.web_tools_summarize(
        {"job_try": 1, "summarize_pipeline": _fake_pipeline}, str(job.id)
    )

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result["summary"] == "A fake but plausible webpage summary."
