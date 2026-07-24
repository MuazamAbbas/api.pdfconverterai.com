"""Full HTTP round trip + ownership + error-envelope conformance for the
files -> jobs -> pdf lifecycle (Handbook Part C.5/C.9, ADR-007).

`POST /v1/files/upload` -> `POST /v1/pdf/convert` -> poll `GET /v1/jobs/{id}`
against the real routers, real Mongo, and real Redis (enqueue only - the
worker task function is invoked directly to process the job, since no
separate `arq` worker process runs during the test suite; see
`tests/test_worker_retry.py`'s module docstring for the same tradeoff).
"""
import os

import pytest

import app.worker as worker
from app.core.storage import STORAGE_PATH

# See tests/test_worker_retry.py's module docstring/comment for why this is
# pinned to the session-scoped loop (Motor's shared `app.core.database.db`
# client).
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _make_file_doc(owner_id, content: bytes, filename: str):
    """Insert a `files` document directly into Mongo, bypassing
    `POST /v1/files/upload` (and therefore `save_uploaded_file`'s upload-time
    validation) entirely. Copied from `tests/test_worker_retry.py`'s helper
    of the same name/shape - see that module's docstring for why direct
    Mongo insertion is the established pattern here rather than mocking.
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
        mimeType="text/plain",
        ownerApiKeyId=owner_id,
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    return FileDocument(**doc)


async def test_upload_convert_poll_round_trip_completes_with_real_content(client, api_key, test_pdf_bytes):
    upload_resp = await client.post(
        "/v1/files/upload",
        files={"file": ("test.pdf", test_pdf_bytes, "application/pdf")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert upload_resp.status_code == 200
    upload_body = upload_resp.json()
    assert upload_body["success"] is True
    assert upload_body["data"]["filename"] == "test.pdf"
    file_id = upload_body["data"]["file_id"]

    convert_resp = await client.post(
        "/v1/pdf/convert",
        json={"file_id": file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    assert convert_resp.status_code == 200
    convert_body = convert_resp.json()
    assert convert_body["success"] is True
    assert convert_body["data"]["status"] == "queued"
    job_id = convert_body["data"]["job_id"]

    # Simulate the arq worker picking the enqueued job up and processing it.
    await worker.pdf_convert({"job_try": 1}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    assert poll_resp.status_code == 200
    poll_body = poll_resp.json()
    assert poll_body["success"] is True
    assert poll_body["data"]["status"] == "completed"
    assert "test PDF" in poll_body["data"]["result"]["text"]


async def test_convert_rejects_nonexistent_file_id(client, api_key):
    resp = await client.post(
        "/v1/pdf/convert",
        json={"file_id": "0123456789ab0123456789ab"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_NOT_FOUND"


async def test_convert_rejects_non_pdf_file(client, api_key):
    """Exercises `_create_pdf_job`'s own `originalFilename` extension check
    (`app/routers/pdf.py`), which is independent of - and runs after -
    `save_uploaded_file`'s upload-time extension/magic-byte validation
    (`app/services/files/service.py`). Since that upload-time validation
    now correctly rejects a `.txt` file before a `file_id` ever exists, this
    can no longer go through `POST /v1/files/upload` (see
    `tests/test_worker_retry.py::_make_file_doc` for the same
    direct-Mongo-insert pattern this borrows). Instead it inserts a
    `files` document directly, bypassing the upload endpoint entirely, so
    `/pdf/convert`'s own defense-in-depth check is what's actually under
    test here, not the upload endpoint's.
    """
    file_doc = await _make_file_doc(api_key["id"], b"just some text, not a pdf", "notes.txt")

    resp = await client.post(
        "/v1/pdf/convert",
        json={"file_id": str(file_doc.id)},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_INVALID_TYPE"


async def test_corrupt_pdf_job_ends_failed_not_retried_over_http(client, api_key, corrupt_pdf_bytes):
    upload_resp = await client.post(
        "/v1/files/upload",
        files={"file": ("corrupt.pdf", corrupt_pdf_bytes, "application/pdf")},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]

    convert_resp = await client.post(
        "/v1/pdf/convert",
        json={"file_id": file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    job_id = convert_resp.json()["data"]["job_id"]

    await worker.pdf_convert({"job_try": 1}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    body = poll_resp.json()
    assert poll_resp.status_code == 200  # polling itself succeeds; the *job* failed
    assert body["data"]["status"] == "failed"
    assert body["data"]["error"]
    assert "Traceback" not in body["data"]["error"]


async def test_download_denied_for_non_owner(client, api_key, other_api_key, test_pdf_bytes):
    upload_resp = await client.post(
        "/v1/files/upload",
        files={"file": ("owned-by-a.pdf", test_pdf_bytes, "application/pdf")},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]

    resp = await client.get(
        f"/v1/files/{file_id}/download", headers={"X-API-Key": other_api_key["key"]}
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_FORBIDDEN"


async def test_owner_can_download_their_own_file(client, api_key, test_pdf_bytes):
    upload_resp = await client.post(
        "/v1/files/upload",
        files={"file": ("owned-by-a.pdf", test_pdf_bytes, "application/pdf")},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]

    resp = await client.get(f"/v1/files/{file_id}/download", headers={"X-API-Key": api_key["key"]})
    assert resp.status_code == 200
    assert resp.content == test_pdf_bytes


async def test_job_poll_denied_for_non_owner(client, api_key, other_api_key, test_pdf_bytes):
    upload_resp = await client.post(
        "/v1/files/upload",
        files={"file": ("owned-by-a.pdf", test_pdf_bytes, "application/pdf")},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]
    convert_resp = await client.post(
        "/v1/pdf/convert", json={"file_id": file_id}, headers={"X-API-Key": api_key["key"]}
    )
    job_id = convert_resp.json()["data"]["job_id"]

    resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": other_api_key["key"]})
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "JOB_FORBIDDEN"


async def test_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.get(
        "/v1/jobs/0123456789ab0123456789ab", headers={"X-API-Key": "not-a-real-key"}
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_missing_api_key_header_rejected(client):
    """A structurally missing `X-API-Key` header (the header is a required
    `Header(...)` with no default) now hits the `RequestValidationError`
    handler registered in `app/main.py` (mirrored here in
    `tests/conftest.py::build_test_app`), so it gets the same Handbook
    Part C.5 `{success: false, message, error: {code}}` envelope as an
    *invalid-value* API key (see
    `test_invalid_api_key_value_rejected_with_envelope` above), instead of
    FastAPI's stock `{"detail": [...]}` 422 shape.
    """
    resp = await client.get("/v1/jobs/0123456789ab0123456789ab")
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
