"""Full HTTP round trip + validation + ownership + error-envelope conformance
for `POST /v1/pdf/split` (Handbook Part C.5/C.9, ADR-007; Handbook Part I.2 -
single-file Tier 2 tool, same shape as `/convert`/`/to_word`/`/summarize`).

Same style as `tests/test_files_jobs_pdf_merge_flow.py`: real routers, real
Mongo, real Redis (enqueue only - the worker task function is invoked
directly to process the job, since no separate `arq` worker process runs
during the test suite).
"""
import os
import zipfile

import pytest
import PyPDF2

import app.worker as worker
from app.core.storage import STORAGE_PATH
from app.services.files.service import get_file_by_id

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _multi_page_pdf_bytes(num_pages: int) -> bytes:
    """Builds a real, valid multi-page PDF in memory via PyPDF2 - `test.pdf`
    (the shared `test_pdf_bytes` fixture) is only 1 page, which isn't enough
    to exercise Split PDF's multi-range success path."""
    from io import BytesIO

    writer = PyPDF2.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def _make_file_doc(owner_id, content: bytes, filename: str, mime_type: str = "text/plain"):
    """Insert a `files` document directly into Mongo, bypassing
    `POST /v1/files/upload`'s upload-time validation entirely - same pattern
    as `tests/test_files_jobs_pdf_flow.py::_make_file_doc`."""
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


async def test_split_happy_path_creates_job_and_completes_with_correct_shape(client, api_key):
    file_id = await _upload_pdf(client, api_key, "multi.pdf", _multi_page_pdf_bytes(5))

    split_resp = await client.post(
        "/v1/pdf/split",
        json={"file_id": file_id, "ranges": "1-2,4"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert split_resp.status_code == 200
    split_body = split_resp.json()
    assert split_body["success"] is True
    assert split_body["data"]["status"] == "queued"
    assert "job_id" in split_body["data"]
    job_id = split_body["data"]["job_id"]

    # Simulate the arq worker picking the enqueued job up and processing it.
    await worker.pdf_split({"job_try": 1}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    assert poll_resp.status_code == 200
    poll_body = poll_resp.json()
    assert poll_body["success"] is True
    assert poll_body["data"]["status"] == "completed"
    output_file_id = poll_body["data"]["result"]["outputFileId"]

    output_doc = await get_file_by_id(output_file_id)
    assert output_doc is not None
    assert output_doc.mimeType == "application/zip"
    with zipfile.ZipFile(output_doc.storagePath) as zf:
        assert sorted(zf.namelist()) == ["split-1.pdf", "split-2.pdf"]
    os.remove(output_doc.storagePath)


async def test_split_rejects_empty_ranges_string(client, api_key, test_pdf_bytes):
    file_id = await _upload_pdf(client, api_key, "empty-ranges.pdf", test_pdf_bytes)

    resp = await client.post(
        "/v1/pdf/split",
        json={"file_id": file_id, "ranges": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "RANGES_INVALID"


async def test_split_rejects_whitespace_only_ranges_string(client, api_key, test_pdf_bytes):
    file_id = await _upload_pdf(client, api_key, "whitespace-ranges.pdf", test_pdf_bytes)

    resp = await client.post(
        "/v1/pdf/split",
        json={"file_id": file_id, "ranges": "   "},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "RANGES_INVALID"


async def test_split_rejects_oversized_ranges_string(client, api_key, test_pdf_bytes):
    """`SplitRequest.ranges` has a 2000-char `max_length` (security-reviewer
    finding: a first line of defense against a huge `ranges` payload, ahead
    of `_parse_ranges`'s `MAX_SPLIT_RANGES`/`MAX_SPLIT_OUTPUT_PAGES` caps).
    Violations are caught by FastAPI's request validation, which
    `app/main.py`'s `RequestValidationError` handler turns into the standard
    error envelope rather than the stock `{"detail": [...]}` shape."""
    file_id = await _upload_pdf(client, api_key, "oversized-ranges.pdf", test_pdf_bytes)

    resp = await client.post(
        "/v1/pdf/split",
        json={"file_id": file_id, "ranges": "1," * 1001},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_split_rejects_file_not_owned_by_caller(client, api_key, other_api_key, test_pdf_bytes):
    other_owned_file_id = await _upload_pdf(client, other_api_key, "not-owned.pdf", test_pdf_bytes)

    resp = await client.post(
        "/v1/pdf/split",
        json={"file_id": other_owned_file_id, "ranges": "1-1"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_FORBIDDEN"


async def test_split_rejects_nonexistent_file_id(client, api_key):
    resp = await client.post(
        "/v1/pdf/split",
        json={"file_id": "0123456789ab0123456789ab", "ranges": "1-1"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_NOT_FOUND"


async def test_split_rejects_non_pdf_file_id(client, api_key):
    """Bypasses `POST /v1/files/upload`'s own extension validation via direct
    Mongo insertion - same pattern as
    `tests/test_files_jobs_pdf_merge_flow.py::test_merge_rejects_non_pdf_file_id`."""
    txt_file_doc = await _make_file_doc(api_key["id"], b"just some text", "notes.txt")

    resp = await client.post(
        "/v1/pdf/split",
        json={"file_id": str(txt_file_doc.id), "ranges": "1-1"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_INVALID_TYPE"


async def test_split_enqueue_failure_returns_503_and_marks_job_failed(
    test_app, client, api_key, test_pdf_bytes, monkeypatch
):
    file_id = await _upload_pdf(client, api_key, "enqueue-fail.pdf", test_pdf_bytes)

    async def _boom(*args, **kwargs):
        raise ConnectionError("simulated redis outage")

    monkeypatch.setattr(test_app.state.arq_redis, "enqueue_job", _boom)

    resp = await client.post(
        "/v1/pdf/split",
        json={"file_id": file_id, "ranges": "1-1"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 503
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "QUEUE_UNAVAILABLE"

    # Same pattern as
    # test_merge_enqueue_failure_returns_503_and_marks_job_failed: the 503
    # response itself doesn't carry a job_id, so look the job up directly by
    # its fileId to confirm the router's mark_failed call before re-raising
    # actually landed.
    from bson import ObjectId

    from app.core.database import db
    from app.schemas.job import JobDocument

    job_doc = await db.jobs.find_one(
        {"fileId": {"$in": [ObjectId(file_id), file_id]}}, sort=[("createdAt", -1)]
    )
    assert job_doc is not None
    job = JobDocument(**job_doc)
    assert job.status.value == "failed"
    assert job.error == "Failed to queue job for processing"


async def test_split_corrupt_pdf_job_ends_failed_not_retried_over_http(
    client, api_key, corrupt_pdf_bytes
):
    upload_resp = await client.post(
        "/v1/files/upload",
        files={"file": ("corrupt.pdf", corrupt_pdf_bytes, "application/pdf")},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]

    split_resp = await client.post(
        "/v1/pdf/split",
        json={"file_id": file_id, "ranges": "1-1"},
        headers={"X-API-Key": api_key["key"]},
    )
    job_id = split_resp.json()["data"]["job_id"]

    await worker.pdf_split({"job_try": 1}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    body = poll_resp.json()
    assert poll_resp.status_code == 200  # polling itself succeeds; the *job* failed
    assert body["data"]["status"] == "failed"
    assert body["data"]["error"]
    assert "Traceback" not in body["data"]["error"]
    assert "startxref" not in body["data"]["error"]
