"""Retry classification contract for `ai_keyword_research` (Handbook Part
C.4, ADR-003; ADR-018 Keyword Research Job System wiring), mirroring
`tests/test_worker_retry_text_web_tools.py`: `app.worker._run_job` (via
`app.worker.ai_keyword_research`) must retry a `TransientProcessingError`
up to `MAX_TRIES`, and must fail a `PermanentProcessingError` immediately
with no retry.

Uses real Mongo `files`/`jobs` documents (real local Mongo, per the task
brief), and `app.worker.ai_keyword_research(ctx, job_id)` is called
directly with a hand-built `ctx` dict standing in for the real ARQ
worker-process context - the same "call the task function directly" style
the sibling retry test modules use. `research_keywords` (the OpenRouter
call `KeywordResearchProcessor.execute()` invokes) is always monkeypatched
at its import site inside `app/services/ai/processors.py::execute` - never
a real network call to OpenRouter.
"""
import os

import pytest
from arq import Retry

import app.worker as worker
from app.core.storage import STORAGE_PATH
from app.schemas.job import JobStatus
from app.services.ai.keyword_research import KeywordResearchUnavailableError
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


_FAKE_RESULT = {
    "seedKeyword": "best running shoes",
    "suggestions": [
        {"keyword": "best running shoes for beginners", "estimatedVolume": 1200, "competition": "medium"},
    ],
}


async def test_permanent_failure_empty_seed_keyword_fails_immediately_no_retry(api_key):
    """An empty/blank seed keyword fails `KeywordResearchProcessor.validate()`
    before `execute()` (and therefore any OpenRouter call) ever runs."""
    file_doc = await _make_text_file_doc(api_key["id"], "   ", "keyword-research-empty.txt")
    job = await create_job(file_doc.id, "ai_keyword_research", api_key["id"])

    await worker.ai_keyword_research({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error and "Traceback" not in updated.error


async def test_permanent_failure_bad_seed_keyword_value_error_fails_no_retry(api_key, monkeypatch):
    """`research_keywords` raising `ValueError` (e.g. over the max length,
    a business rule `validate()` doesn't re-check) is a permanent,
    non-retryable failure - matches `TextParaphraseProcessor`'s ValueError
    handling."""
    file_doc = await _make_text_file_doc(api_key["id"], "some seed keyword", "keyword-research-value-error.txt")
    job = await create_job(file_doc.id, "ai_keyword_research", api_key["id"])

    async def _fake_research_keywords(seed_keyword):
        raise ValueError("Seed keyword must be at most 200 characters")

    monkeypatch.setattr(
        "app.services.ai.keyword_research.research_keywords", _fake_research_keywords
    )

    await worker.ai_keyword_research({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Seed keyword must be a valid, non-empty string"


async def test_permanent_failure_openrouter_unavailable_fails_no_retry(api_key, monkeypatch):
    """`KeywordResearchUnavailableError` (OpenRouter down/circuit breaker
    tripped/unparseable response) is deliberately mapped to
    `PermanentProcessingError`, not `TransientProcessingError` - a retry
    within this job's short lifetime won't fix a tripped 5-minute circuit
    breaker cooldown, matching the class docstring's documented
    expectation."""
    file_doc = await _make_text_file_doc(api_key["id"], "some seed keyword", "keyword-research-unavailable.txt")
    job = await create_job(file_doc.id, "ai_keyword_research", api_key["id"])

    async def _fake_research_keywords(seed_keyword):
        raise KeywordResearchUnavailableError("OpenRouter is temporarily unavailable")

    monkeypatch.setattr(
        "app.services.ai.keyword_research.research_keywords", _fake_research_keywords
    )

    await worker.ai_keyword_research({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Keyword research is temporarily unavailable, please try again shortly"


async def test_transient_failure_unexpected_error_retries(api_key, monkeypatch):
    """A genuinely unexpected exception from `research_keywords` (not
    `ValueError`/`KeywordResearchUnavailableError`) is treated as transient
    and retried - matches `TextParaphraseProcessor`'s catch-all `Exception`
    handling."""
    file_doc = await _make_text_file_doc(api_key["id"], "some seed keyword", "keyword-research-unexpected.txt")
    job = await create_job(file_doc.id, "ai_keyword_research", api_key["id"])

    async def _fake_research_keywords(seed_keyword):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(
        "app.services.ai.keyword_research.research_keywords", _fake_research_keywords
    )

    with pytest.raises(Retry):
        await worker.ai_keyword_research({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_transient_failure_exhausted_retries_marks_failed_no_more_retry(api_key, monkeypatch):
    file_doc = await _make_text_file_doc(api_key["id"], "some seed keyword", "keyword-research-exhausted.txt")
    job = await create_job(file_doc.id, "ai_keyword_research", api_key["id"])

    async def _fake_research_keywords(seed_keyword):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(
        "app.services.ai.keyword_research.research_keywords", _fake_research_keywords
    )

    await worker.ai_keyword_research({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Processing failed after multiple attempts"


async def test_successful_keyword_research_completes(api_key, monkeypatch):
    file_doc = await _make_text_file_doc(api_key["id"], "best running shoes", "keyword-research-success.txt")
    job = await create_job(file_doc.id, "ai_keyword_research", api_key["id"])

    async def _fake_research_keywords(seed_keyword):
        assert seed_keyword == "best running shoes"
        return _FAKE_RESULT

    monkeypatch.setattr(
        "app.services.ai.keyword_research.research_keywords", _fake_research_keywords
    )

    await worker.ai_keyword_research({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result["seedKeyword"] == "best running shoes"
    assert updated.result["suggestions"] == _FAKE_RESULT["suggestions"]
