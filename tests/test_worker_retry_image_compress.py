"""Retry classification + full job-lifecycle contract for `image_compress`
(Handbook Part C.4, ADR-003), mirroring `tests/test_worker_retry_image.py`'s
pattern for `image_ocr` applied to the `image` module's second Tier 2 job:
`app.worker._run_job` (via the `image_compress` task function) must retry a
`TransientProcessingError` up to `MAX_TRIES`, and must fail a
`PermanentProcessingError` immediately with no retry.

Against real local Mongo (`create_job`/`get_job` - same as
`tests/test_worker_retry_image.py`) but *not* through the HTTP
`client`/`test_app` fixture (`tests/conftest.py`) - that fixture creates a
real `arq` Redis connection pool at fixture setup even for endpoints that
never enqueue anything, which isn't reachable in this checkout (see
`tests/test_files_jobs_image_flow.py`'s equivalent tradeoff note for
`image_ocr` - no separate `arq` worker process runs during the test suite
either way). Calling `app.worker.image_compress` directly exercises the
exact same `_run_job` orchestration a real worker process would, without
needing either Redis or a second process.

`os.path.getsize` (imported at `app.services.image.processors`' module
scope) is monkeypatched to force the transient-I/O-error path on demand,
the same technique `tests/test_image_compress_processor.py` uses at the
unit level - a real transient I/O fault isn't practical to reproduce here.
"""
import os

import pytest
from arq import Retry

import app.worker as worker
from app.core.storage import STORAGE_PATH
from app.schemas.job import JobStatus
from app.services.jobs.service import create_job, get_job

# See tests/test_worker_retry_image.py's comment: Motor's shared
# `app.core.database.db` client must stay bound to one event loop for the
# whole session.
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_file_doc(owner_id, content: bytes, filename: str, mime_type: str = "image/jpeg"):
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
        mimeType=mime_type,
        ownerApiKeyId=owner_id,
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    return FileDocument(**doc)


def _jpeg_bytes(size=(120, 120), quality=95) -> bytes:
    import io

    from PIL import Image

    img = Image.new("RGB", size)
    for x in range(size[0]):
        for y in range(size[1]):
            img.putpixel((x, y), ((x * 37) % 256, (y * 53) % 256, ((x + y) * 17) % 256))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


async def _cleanup_job_output(job_id: str) -> None:
    """Best-effort removal of any files this test caused to be written
    under `STORAGE_PATH` (both the `image_compress` processor's own
    `compress-{job.id}.*` output and, if the job completed, the
    `save_output_file`-registered `files` doc/disk copy it produced) -
    mirrors `tests/conftest.py::_cleanup_api_key`'s cleanup discipline of
    only ever touching what this test run created."""
    from app.core.database import db

    for ext in (".jpg", ".png"):
        path = os.path.join(STORAGE_PATH, f"compress-{job_id}{ext}")
        if os.path.exists(path):
            os.remove(path)

    job = await get_job(job_id)
    if job and job.result and job.result.get("outputFileId"):
        from bson import ObjectId

        out_doc = await db.files.find_one({"_id": ObjectId(job.result["outputFileId"])})
        if out_doc:
            out_path = out_doc.get("storagePath")
            if out_path and os.path.exists(out_path):
                os.remove(out_path)
            await db.files.delete_one({"_id": out_doc["_id"]})


async def test_compress_job_completes_end_to_end_with_expected_result_shape(api_key):
    """Full Validate -> Prepare -> Execute -> Verify -> Complete lifecycle
    for a real, valid JPEG - the "end-to-end job lifecycle" case, driven
    directly through `create_job`/the worker task function/`get_job`
    (Handbook Part C.7) rather than through HTTP."""
    file_doc = await _make_file_doc(api_key["id"], _jpeg_bytes(), "compress-e2e.jpg")
    job = await create_job(file_doc.id, "image_compress", api_key["id"], params={"level": "high"})

    await worker.image_compress({"job_try": 1}, str(job.id))

    try:
        updated = await get_job(str(job.id))
        assert updated.status == JobStatus.COMPLETED
        assert updated.result["alreadyOptimal"] is False
        assert updated.result["originalSize"] > updated.result["compressedSize"] > 0
        assert updated.result["outputFileId"]
    finally:
        await _cleanup_job_output(str(job.id))


async def test_compress_job_already_optimal_completes_successfully_not_a_failure(
    api_key, monkeypatch
):
    """The already-optimal case (Handbook Part I.2 spec) must complete the
    Job as COMPLETED, never FAILED - `alreadyOptimal: true` is a flag on a
    successful result, not an error."""
    file_doc = await _make_file_doc(api_key["id"], _jpeg_bytes(), "compress-optimal.jpg")
    job = await create_job(file_doc.id, "image_compress", api_key["id"], params={"level": "low"})

    def _fake_getsize(path):
        if path == file_doc.storagePath:
            return 100
        return 150

    monkeypatch.setattr("app.services.image.processors.os.path.getsize", _fake_getsize)

    await worker.image_compress({"job_try": 1}, str(job.id))
    monkeypatch.undo()

    try:
        updated = await get_job(str(job.id))
        assert updated.status == JobStatus.COMPLETED, "already-optimal must not fail the job"
        assert updated.result["alreadyOptimal"] is True
        assert updated.result["originalSize"] == 100
        assert updated.result["compressedSize"] == 150
    finally:
        await _cleanup_job_output(str(job.id))


async def test_permanent_failure_corrupt_image_fails_immediately_no_retry(api_key):
    file_doc = await _make_file_doc(
        api_key["id"], b"not a real jpeg, just garbage bytes", "compress-corrupt.jpg"
    )
    job = await create_job(file_doc.id, "image_compress", api_key["id"], params={"level": "medium"})

    # job_try=1: even on a fresh, first attempt, a permanent error must not retry.
    await worker.image_compress({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "permanent failures must never consume/increment retries"
    assert updated.error == "File is not a valid image"


async def test_transient_failure_retries_below_max_tries(api_key, monkeypatch):
    file_doc = await _make_file_doc(api_key["id"], _jpeg_bytes(), "compress-transient.jpg")
    job = await create_job(file_doc.id, "image_compress", api_key["id"], params={"level": "medium"})

    def _boom(path):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.image.processors.os.path.getsize", _boom)

    with pytest.raises(Retry):
        await worker.image_compress({"job_try": 1}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.PROCESSING
    assert updated.retryCount == 1


async def test_transient_failure_exhausted_retries_marks_failed_no_more_retry(api_key, monkeypatch):
    file_doc = await _make_file_doc(api_key["id"], _jpeg_bytes(), "compress-exhausted.jpg")
    job = await create_job(file_doc.id, "image_compress", api_key["id"], params={"level": "medium"})

    def _boom(path):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.image.processors.os.path.getsize", _boom)

    # job_try == MAX_TRIES: this is the last allowed attempt - must fail, not retry again.
    await worker.image_compress({"job_try": worker.MAX_TRIES}, str(job.id))

    updated = await get_job(str(job.id))
    assert updated.status == JobStatus.FAILED
    assert updated.retryCount == 0, "the final exhausted attempt marks failed, not increment+retry"
    assert updated.error == "Processing failed after multiple attempts"


async def test_successful_compress_after_transient_retry_completes(api_key, monkeypatch):
    """Sanity check the retry path isn't a dead end: once the transient
    condition clears, the next attempt (a fresh `_run_job` call, as the real
    `arq` worker would do after `Retry`) completes normally."""
    file_doc = await _make_file_doc(api_key["id"], _jpeg_bytes(), "compress-recovers.jpg")
    job = await create_job(file_doc.id, "image_compress", api_key["id"], params={"level": "medium"})

    calls = {"n": 0}
    real_getsize = os.path.getsize

    def _flaky_once(path):
        if path == file_doc.storagePath:
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("simulated transient disk hiccup")
        return real_getsize(path)

    monkeypatch.setattr("app.services.image.processors.os.path.getsize", _flaky_once)

    with pytest.raises(Retry):
        await worker.image_compress({"job_try": 1}, str(job.id))

    # Second attempt, as arq would perform after the Retry.
    await worker.image_compress({"job_try": 2}, str(job.id))
    monkeypatch.undo()

    try:
        updated = await get_job(str(job.id))
        assert updated.status == JobStatus.COMPLETED
        assert updated.result["outputFileId"]
    finally:
        await _cleanup_job_output(str(job.id))
