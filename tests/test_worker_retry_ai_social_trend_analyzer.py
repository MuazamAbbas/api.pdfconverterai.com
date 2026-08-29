"""Retry classification contract for `ai_social_trend_analyzer` (Handbook
Part C.4, ADR-003; ADR-018 Social Trend Analyzer Job System wiring),
mirroring `tests/test_worker_retry_ai_content_idea_generator.py` exactly:
`app.worker._run_job` (via `app.worker.ai_social_trend_analyzer`) must retry
a `TransientProcessingError` up to `MAX_TRIES`, and must fail a
`PermanentProcessingError` immediately with no retry.

Uses real Mongo `files`/`jobs` documents, and
`app.worker.ai_social_trend_analyzer(ctx, job_id)` is called directly with a
hand-built `ctx` dict standing in for the real ARQ worker-process context.
`generate_social_trends` (the OpenRouter call
`SocialTrendAnalyzerProcessor.execute()` invokes) is always monkeypatched at
its import site inside `app/services/ai/processors.py::execute` - never a
real network call to OpenRouter.
"""
import os

import pytest
from arq import Retry

import app.worker as worker
from app.core.storage import STORAGE_PATH
from app.schemas.job import JobStatus
from app.services.ai.social_trend_analyzer import SocialTrendAnalyzerUnavailableError
from app.services.jobs.service import create_job, get_job

# `app.core.database.db` is a Motor client created once at import time and
# must stay bound to a single event loop for the whole session - pin these
# tests to that same session-scoped loop rather than pytest-asyncio's
# per-test default, same as the content idea generator retry test module.
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
    "topic": "electric bikes",
    "hashtags": ["#ElectricBikes", "#EBike"],
    "contentIdeas": ["Post a Reel of your daily commute on an e-bike"],
}


async def test_permanent_failure_empty_topic_fails_immediately_no_retry(api_key):
    """An empty/blank topic fails `SocialTrendAnalyzerProcessor.validate()`
    before `execute()` (and therefore any OpenRouter call) ever runs."""
    file_doc = await _make_text_file_doc(api_key["id"], "   ", "social-trend-empty.txt")
    job = await create_job(file_doc.id, "ai_social_trend_analyzer", api_key["id"])

    await worker.ai_social_trend_analyzer({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error and "Traceback" not in updated.error


async def test_permanent_failure_bad_topic_value_error_fails_no_retry(api_key, monkeypatch):
    """`generate_social_trends` raising `ValueError` (e.g. over the max
    length, a business rule `validate()` doesn't re-check) is a permanent,
    non-retryable failure - matches `ContentIdeaGeneratorProcessor`'s
    ValueError handling."""
    file_doc = await _make_text_file_doc(api_key["id"], "some topic", "social-trend-value-error.txt")
    job = await create_job(file_doc.id, "ai_social_trend_analyzer", api_key["id"])

    async def _fake_generate_social_trends(topic):
        raise ValueError("Topic must be at most 200 characters")

    monkeypatch.setattr(
        "app.services.ai.social_trend_analyzer.generate_social_trends",
        _fake_generate_social_trends,
    )

    await worker.ai_social_trend_analyzer({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Topic must be a valid, non-empty string"


async def test_permanent_failure_openrouter_unavailable_fails_no_retry(api_key, monkeypatch):
    """`SocialTrendAnalyzerUnavailableError` (OpenRouter down/circuit
    breaker tripped/unparseable response) is deliberately mapped to
    `PermanentProcessingError`, not `TransientProcessingError` - matches
    `ContentIdeaGeneratorProcessor`'s equivalent handling."""
    file_doc = await _make_text_file_doc(api_key["id"], "some topic", "social-trend-unavailable.txt")
    job = await create_job(file_doc.id, "ai_social_trend_analyzer", api_key["id"])

    async def _fake_generate_social_trends(topic):
        raise SocialTrendAnalyzerUnavailableError("OpenRouter is temporarily unavailable")

    monkeypatch.setattr(
        "app.services.ai.social_trend_analyzer.generate_social_trends",
        _fake_generate_social_trends,
    )

    await worker.ai_social_trend_analyzer({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Social trend analysis is temporarily unavailable, please try again shortly"


async def test_transient_failure_unexpected_error_retries(api_key, monkeypatch):
    """A genuinely unexpected exception from `generate_social_trends` (not
    `ValueError`/`SocialTrendAnalyzerUnavailableError`) is treated as
    transient and retried."""
    file_doc = await _make_text_file_doc(api_key["id"], "some topic", "social-trend-unexpected.txt")
    job = await create_job(file_doc.id, "ai_social_trend_analyzer", api_key["id"])

    async def _fake_generate_social_trends(topic):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(
        "app.services.ai.social_trend_analyzer.generate_social_trends",
        _fake_generate_social_trends,
    )

    with pytest.raises(Retry):
        await worker.ai_social_trend_analyzer({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_transient_failure_exhausted_retries_marks_failed_no_more_retry(api_key, monkeypatch):
    file_doc = await _make_text_file_doc(api_key["id"], "some topic", "social-trend-exhausted.txt")
    job = await create_job(file_doc.id, "ai_social_trend_analyzer", api_key["id"])

    async def _fake_generate_social_trends(topic):
        raise RuntimeError("simulated unexpected failure")

    monkeypatch.setattr(
        "app.services.ai.social_trend_analyzer.generate_social_trends",
        _fake_generate_social_trends,
    )

    await worker.ai_social_trend_analyzer({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Processing failed after multiple attempts"


async def test_successful_social_trend_analyzer_completes(api_key, monkeypatch):
    file_doc = await _make_text_file_doc(
        api_key["id"], "electric bikes", "social-trend-success.txt"
    )
    job = await create_job(file_doc.id, "ai_social_trend_analyzer", api_key["id"])

    async def _fake_generate_social_trends(topic):
        assert topic == "electric bikes"
        return _FAKE_RESULT

    monkeypatch.setattr(
        "app.services.ai.social_trend_analyzer.generate_social_trends",
        _fake_generate_social_trends,
    )

    await worker.ai_social_trend_analyzer({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result["topic"] == "electric bikes"
    assert updated.result["hashtags"] == _FAKE_RESULT["hashtags"]
    assert updated.result["contentIdeas"] == _FAKE_RESULT["contentIdeas"]


async def test_verify_rejects_result_missing_a_category(api_key, monkeypatch):
    """`SocialTrendAnalyzerProcessor.verify()` guards against a result
    silently missing one of the two named categories - the risk flagged in
    the approved feature-spec for this tool's 2-key object shape (as
    opposed to Keyword Research's flat array, where "missing" just means an
    empty list)."""
    file_doc = await _make_text_file_doc(
        api_key["id"], "electric bikes", "social-trend-incomplete.txt"
    )
    job = await create_job(file_doc.id, "ai_social_trend_analyzer", api_key["id"])

    incomplete_result = {
        "topic": "electric bikes",
        "hashtags": ["#ElectricBikes"],
        "contentIdeas": [],
    }

    async def _fake_generate_social_trends(topic):
        return incomplete_result

    monkeypatch.setattr(
        "app.services.ai.social_trend_analyzer.generate_social_trends",
        _fake_generate_social_trends,
    )

    await worker.ai_social_trend_analyzer({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Social trend analysis produced an incomplete result"
