"""Retry classification contract for `downloaders_youtube` (Handbook Part
C.4, ADR-003; downloaders/youtube Job System wiring), mirroring
`tests/test_worker_retry_text_web_tools.py`: `app.worker._run_job` (via
`app.worker.downloaders_youtube`) must retry a `TransientProcessingError` up
to `MAX_TRIES`, and must fail a `PermanentProcessingError` immediately with
no retry - including the message-substring classification
`DownloadersYoutubeProcessor.execute()` does against yt_dlp's single
`DownloadError` exception class (`_PERMANENT_ERROR_MARKERS`), and the
zero-byte/missing-output rejection its `verify()` does.

Uses real Mongo `files`/`jobs` documents (real local Mongo, per the task
brief), and `app.worker.downloaders_youtube(ctx, job_id)` is called directly
with a hand-built `ctx` dict standing in for the real ARQ worker-process
context - the same "call the task function directly" style the sibling
retry test modules use. The real `yt_dlp.YoutubeDL`/network download is
never exercised - `yt_dlp.YoutubeDL` itself is monkeypatched (the same
import site `DownloadersYoutubeProcessor.execute()` uses), never a real
`yt_dlp` download.
"""
import os

import pytest
import yt_dlp
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


async def _make_url_file_doc(owner_id, url: str, filename: str):
    import hashlib

    from app.core.database import db
    from app.schemas.file import FileCreate, FileDocument

    os.makedirs(STORAGE_PATH, exist_ok=True)
    storage_path = os.path.join(STORAGE_PATH, filename)
    with open(storage_path, "w", encoding="utf-8") as f:
        f.write(url)
    encoded = url.encode("utf-8")

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


_A_YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class _RaisingYoutubeDL:
    """Stands in for `yt_dlp.YoutubeDL`, raising `message` as a
    `yt_dlp.utils.DownloadError` from `extract_info` - the single exception
    type real yt_dlp collapses nearly every failure mode into (see
    `app/services/downloaders/processors.py`'s `_PERMANENT_ERROR_MARKERS`
    comment)."""

    def __init__(self, message: str):
        self._message = message

    def __call__(self, opts):
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=True):
        raise yt_dlp.utils.DownloadError(self._message)


class _EmptyOutputYoutubeDL:
    """Stands in for `yt_dlp.YoutubeDL` - "succeeds" per yt_dlp but never
    actually writes any bytes to `output_path`, driving
    `DownloadersYoutubeProcessor.verify()`'s zero-byte/missing-output
    rejection rather than `execute()`'s own DownloadError handling."""

    def __init__(self, opts):
        self._opts = opts

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def extract_info(self, url, download=True):
        return {"id": "emptyid", "ext": "mp4", "title": "Never Actually Downloaded"}

    def prepare_filename(self, info):
        # A path inside the real output_dir that is simply never written to.
        output_dir = os.path.dirname(self._opts["outtmpl"])
        return os.path.join(output_dir, "emptyid.mp4")


async def test_transient_network_error_retries_instead_of_failing(api_key, monkeypatch):
    file_doc = await _make_url_file_doc(api_key["id"], _A_YOUTUBE_URL, "yt-transient.txt")
    job = await create_job(file_doc.id, "downloaders_youtube")

    monkeypatch.setattr(
        "yt_dlp.YoutubeDL", _RaisingYoutubeDL("some generic network error")
    )

    with pytest.raises(Retry):
        await worker.downloaders_youtube({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_transient_failure_exhausted_retries_marks_failed_no_more_retry(api_key, monkeypatch):
    file_doc = await _make_url_file_doc(api_key["id"], _A_YOUTUBE_URL, "yt-exhausted.txt")
    job = await create_job(file_doc.id, "downloaders_youtube")

    monkeypatch.setattr(
        "yt_dlp.YoutubeDL", _RaisingYoutubeDL("some generic network error")
    )

    await worker.downloaders_youtube({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error == "Processing failed after multiple attempts"


async def test_permanent_failure_video_unavailable_fails_immediately_no_retry(api_key, monkeypatch):
    file_doc = await _make_url_file_doc(api_key["id"], _A_YOUTUBE_URL, "yt-unavailable.txt")
    job = await create_job(file_doc.id, "downloaders_youtube")

    monkeypatch.setattr("yt_dlp.YoutubeDL", _RaisingYoutubeDL("Video unavailable"))

    # A permanent failure must not raise arq.Retry at all.
    await worker.downloaders_youtube({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error and "Traceback" not in updated.error


async def test_permanent_failure_bad_url_input_fails_immediately_no_retry(api_key):
    """`DownloadersYoutubeProcessor.validate()` rejects a non-http(s) URL
    before `execute()` (and therefore any yt_dlp call) ever runs."""
    file_doc = await _make_url_file_doc(api_key["id"], "ftp://example.com/video", "yt-bad-url.txt")
    job = await create_job(file_doc.id, "downloaders_youtube")

    await worker.downloaders_youtube({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error and "Traceback" not in updated.error


async def test_verify_rejects_zero_byte_missing_output_as_permanent_no_retry(api_key, monkeypatch):
    file_doc = await _make_url_file_doc(api_key["id"], _A_YOUTUBE_URL, "yt-empty-output.txt")
    job = await create_job(file_doc.id, "downloaders_youtube")

    monkeypatch.setattr("yt_dlp.YoutubeDL", _EmptyOutputYoutubeDL)

    await worker.downloaders_youtube({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0
    assert updated.error and "Traceback" not in updated.error
    assert "no output" in updated.error.lower() or "output file" in updated.error.lower()


async def test_successful_download_completes_with_registered_output_file(api_key, monkeypatch):
    file_doc = await _make_url_file_doc(api_key["id"], _A_YOUTUBE_URL, "yt-success.txt")
    job = await create_job(file_doc.id, "downloaders_youtube")

    class _SucceedingYoutubeDL:
        def __init__(self, opts):
            self._opts = opts
            self._output_path = None

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def extract_info(self, url, download=True):
            output_dir = os.path.dirname(self._opts["outtmpl"])
            output_path = os.path.join(output_dir, "goodid.mp4")
            with open(output_path, "wb") as f:
                f.write(b"a real-enough fake mp4 payload")
            self._output_path = output_path
            return {"id": "goodid", "ext": "mp4", "title": "A Good Fake Video"}

        def prepare_filename(self, info):
            return self._output_path

    monkeypatch.setattr("yt_dlp.YoutubeDL", _SucceedingYoutubeDL)

    await worker.downloaders_youtube({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.COMPLETED
    assert updated.result["outputFileId"]
