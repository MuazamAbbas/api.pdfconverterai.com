"""Full HTTP round trip + validation + ownership + error-envelope conformance
for `POST /v1/pdf/merge` (Handbook Part C.5/C.9, ADR-007; Handbook Part I.2 -
the first multi-file Tier 2 tool).

Same style as `tests/test_files_jobs_pdf_flow.py`: real routers, real Mongo,
real Redis (enqueue only - the worker task function is invoked directly to
process the job, since no separate `arq` worker process runs during the test
suite).
"""
import os

import pytest

import app.worker as worker
from app.core.storage import STORAGE_PATH
from app.services.files.service import get_file_by_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_file_doc(owner_id, content: bytes, filename: str, mime_type: str = "text/plain"):
    """Insert a `files` document directly into Mongo, bypassing
    `POST /v1/files/upload`'s upload-time validation entirely - same pattern
    as `tests/test_files_jobs_pdf_flow.py::_make_file_doc`.
    """
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


async def _upload_pdf(client, api_key, filename, content):
    resp = await client.post(
        "/v1/files/upload",
        files={"file": (filename, content, "application/pdf")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    return resp.json()["data"]["file_id"]


async def test_merge_happy_path_creates_job_and_completes_with_correct_shape(
    client, api_key, test_pdf_bytes
):
    file_id_a = await _upload_pdf(client, api_key, "a.pdf", test_pdf_bytes)
    file_id_b = await _upload_pdf(client, api_key, "b.pdf", test_pdf_bytes)

    merge_resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": [file_id_a, file_id_b]},
        headers={"X-API-Key": api_key["key"]},
    )
    assert merge_resp.status_code == 200
    merge_body = merge_resp.json()
    assert merge_body["success"] is True
    assert merge_body["data"]["status"] == "queued"
    assert "job_id" in merge_body["data"]
    job_id = merge_body["data"]["job_id"]

    # Simulate the arq worker picking the enqueued job up and processing it.
    await worker.pdf_merge({"job_try": 1}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    assert poll_resp.status_code == 200
    poll_body = poll_resp.json()
    assert poll_body["success"] is True
    assert poll_body["data"]["status"] == "completed"
    output_file_id = poll_body["data"]["result"]["outputFileId"]

    output_doc = await get_file_by_id(output_file_id)
    assert output_doc is not None
    os.remove(output_doc.storagePath)


async def test_merge_rejects_fewer_than_two_files(client, api_key, test_pdf_bytes):
    file_id_a = await _upload_pdf(client, api_key, "solo.pdf", test_pdf_bytes)

    resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": [file_id_a]},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_COUNT_INVALID"


async def test_merge_rejects_zero_files(client, api_key):
    resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": []},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_COUNT_INVALID"


async def test_merge_rejects_more_than_twenty_files(client, api_key):
    # Count check runs before any file lookup, so these don't need to exist.
    fake_ids = [f"{i:024x}" for i in range(21)]
    resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": fake_ids},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_COUNT_INVALID"


async def test_merge_rejects_duplicate_file_ids(client, api_key, test_pdf_bytes):
    file_id_a = await _upload_pdf(client, api_key, "dup.pdf", test_pdf_bytes)

    resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": [file_id_a, file_id_a]},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_COUNT_INVALID"


async def test_merge_rejects_file_not_owned_by_caller(
    client, api_key, other_api_key, test_pdf_bytes
):
    owned_file_id = await _upload_pdf(client, api_key, "owned.pdf", test_pdf_bytes)
    other_owned_file_id = await _upload_pdf(client, other_api_key, "not-owned.pdf", test_pdf_bytes)

    resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": [owned_file_id, other_owned_file_id]},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_FORBIDDEN"


async def test_merge_rejects_non_pdf_file_id(client, api_key, test_pdf_bytes):
    """Bypasses `POST /v1/files/upload`'s own extension validation via direct
    Mongo insertion - same pattern as
    `tests/test_files_jobs_pdf_flow.py::test_convert_rejects_non_pdf_file`."""
    pdf_file_id = await _upload_pdf(client, api_key, "real.pdf", test_pdf_bytes)
    txt_file_doc = await _make_file_doc(api_key["id"], b"just some text", "notes.txt")

    resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": [pdf_file_id, str(txt_file_doc.id)]},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_INVALID_TYPE"


async def test_merge_rejects_nonexistent_file_id(client, api_key, test_pdf_bytes):
    pdf_file_id = await _upload_pdf(client, api_key, "real2.pdf", test_pdf_bytes)

    resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": [pdf_file_id, "0123456789ab0123456789ab"]},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_NOT_FOUND"


async def test_merge_rejects_combined_size_over_aggregate_cap(client, api_key):
    """Per-file size is already bounded at upload time; this covers the
    aggregate cap added for the security-reviewer finding that up to 20
    files at the per-file max could otherwise reach ~500MB in one job.

    Inserts `files` docs with a declared `sizeBytes` far above the real
    on-disk content (tiny, since this request 400s before any content is
    read) - only the router's `sum(file_doc.sizeBytes ...)` check is under
    test here, not actual byte-for-byte upload/merge behavior.
    """
    import hashlib

    from app.core.database import db
    from app.schemas.file import FileCreate, FileDocument

    os.makedirs(STORAGE_PATH, exist_ok=True)
    file_ids = []
    for i in range(2):
        filename = f"oversized-{i}.pdf"
        storage_path = os.path.join(STORAGE_PATH, filename)
        with open(storage_path, "wb") as f:
            f.write(b"%PDF-1.4\n%%EOF")
        file_create = FileCreate(
            storagePath=storage_path,
            checksum=hashlib.sha256(b"declared-oversized").hexdigest(),
            originalFilename=filename,
            sizeBytes=200 * 1024 * 1024,  # 200MB declared; two of these > 250MB cap
            mimeType="application/pdf",
            ownerApiKeyId=api_key["id"],
        )
        insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
        doc = await db.files.find_one({"_id": insert_result.inserted_id})
        file_ids.append(str(FileDocument(**doc).id))

    resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": file_ids},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_COUNT_INVALID"


async def test_merge_enqueue_failure_returns_503_and_marks_job_failed(
    test_app, client, api_key, test_pdf_bytes, monkeypatch
):
    file_id_a = await _upload_pdf(client, api_key, "enqueue-fail-a.pdf", test_pdf_bytes)
    file_id_b = await _upload_pdf(client, api_key, "enqueue-fail-b.pdf", test_pdf_bytes)

    async def _boom(*args, **kwargs):
        raise ConnectionError("simulated redis outage")

    monkeypatch.setattr(test_app.state.arq_redis, "enqueue_job", _boom)

    resp = await client.post(
        "/v1/pdf/merge",
        json={"file_ids": [file_id_a, file_id_b]},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "QUEUE_UNAVAILABLE"

    # The 503 response itself doesn't carry a job_id (the failure happens
    # after job creation, before the router can return one) - look the job
    # up directly by its `fileId` (set to file_id_a, the first submitted
    # file) to confirm the router's `mark_failed` call before re-raising
    # actually landed, same as the single-file `_create_pdf_job` contract.
    from bson import ObjectId

    from app.core.database import db
    from app.schemas.job import JobDocument

    job_doc = await db.jobs.find_one(
        {"fileId": {"$in": [ObjectId(file_id_a), file_id_a]}}, sort=[("createdAt", -1)]
    )
    assert job_doc is not None
    job = JobDocument(**job_doc)
    assert job.status.value == "failed"
    assert job.error == "Failed to queue job for processing"
