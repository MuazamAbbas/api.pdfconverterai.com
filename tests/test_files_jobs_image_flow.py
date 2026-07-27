"""Full HTTP round trip + ownership + error-envelope conformance for the
files -> jobs -> image lifecycle (Handbook Part C.5/C.9), mirroring
`tests/test_files_jobs_pdf_flow.py` exactly for the `image` module's
`POST /image/upload` -> `POST /image/ocr` -> poll `GET /jobs/{id}` flow
(`app/routers/image.py`'s reconciliation onto the shared Job System).

Against the real routers, real Mongo, and real Redis (enqueue only - the
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


def _tesseract_available() -> bool:
    """`app/services/image/ocr.py` hardcodes
    `pytesseract.pytesseract.tesseract_cmd = "/usr/bin/tesseract"` (a
    Linux-only absolute path, not overridable via PATH/env) as an
    import-time side effect. That's fine on a Linux box that actually has
    Tesseract installed at that exact path (e.g. the target VPS), but makes
    the real-OCR round trip impossible to exercise on this Windows dev
    checkout - there is no way to point it at a Windows Tesseract install
    even if one is present. Skip (not fail) the one test that needs a real
    OCR binary rather than weakening it into a mock; every other test in
    this module does not depend on Tesseract at all.
    """
    import app.services.image.ocr  # noqa: F401  (sets pytesseract.pytesseract.tesseract_cmd)
    import pytesseract

    try:
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


async def _make_file_doc(owner_id, content: bytes, filename: str, mime_type: str = "text/plain"):
    """Insert a `files` document directly into Mongo, bypassing
    `POST /v1/image/upload` (and therefore `save_uploaded_file`'s
    upload-time validation) entirely. Copied from
    `tests/test_files_jobs_pdf_flow.py`'s helper of the same name/shape -
    see that module's docstring for why direct Mongo insertion is the
    established pattern here rather than mocking.
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


@pytest.mark.skipif(
    not _tesseract_available(),
    reason="Real `tesseract` binary not reachable at the hardcoded "
    "/usr/bin/tesseract path in app/services/image/ocr.py on this checkout "
    "(see _tesseract_available's docstring above) - this exercises the "
    "actual OCR pipeline, not a mock, so it's skipped rather than weakened.",
)
async def test_upload_ocr_poll_round_trip_completes_with_real_content(
    client, api_key, image_with_text_bytes
):
    upload_resp = await client.post(
        "/v1/image/upload",
        files={"file": ("hello.png", image_with_text_bytes, "image/png")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert upload_resp.status_code == 200
    upload_body = upload_resp.json()
    assert upload_body["success"] is True
    assert upload_body["data"]["filename"] == "hello.png"
    file_id = upload_body["data"]["file_id"]

    ocr_resp = await client.post(
        "/v1/image/ocr",
        json={"file_id": file_id},
        headers={"X-API-Key": api_key["key"]},
    )
    assert ocr_resp.status_code == 200
    ocr_body = ocr_resp.json()
    assert ocr_body["success"] is True
    assert ocr_body["data"]["status"] == "queued"
    job_id = ocr_body["data"]["job_id"]

    # Simulate the arq worker picking the enqueued job up and processing it.
    await worker.image_ocr({"job_try": 1}, job_id)

    poll_resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": api_key["key"]})
    assert poll_resp.status_code == 200
    poll_body = poll_resp.json()
    assert poll_body["success"] is True
    assert poll_body["data"]["status"] == "completed"
    assert "HELLO" in poll_body["data"]["result"]["text"].upper()


async def test_ocr_rejects_nonexistent_file_id(client, api_key):
    resp = await client.post(
        "/v1/image/ocr",
        json={"file_id": "0123456789ab0123456789ab"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 404
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_NOT_FOUND"


async def test_ocr_rejects_non_image_file(client, api_key):
    """Exercises `_create_image_job`'s own `originalFilename` extension check
    (`app/routers/image.py`), which is independent of - and runs after -
    `save_uploaded_file`'s upload-time extension/magic-byte validation
    (`app/services/files/service.py`). Since that upload-time validation
    now correctly rejects a `.txt` file before a `file_id` ever exists, this
    can no longer go through `POST /v1/image/upload` (see
    `test_convert_rejects_non_pdf_file` in `test_files_jobs_pdf_flow.py` for
    the exact same pattern applied to `pdf`). Instead it inserts a `files`
    document directly, bypassing the upload endpoint entirely, so
    `/image/ocr`'s own defense-in-depth check is what's actually under test
    here, not the upload endpoint's.
    """
    file_doc = await _make_file_doc(api_key["id"], b"just some text, not an image", "notes.txt")

    resp = await client.post(
        "/v1/image/ocr",
        json={"file_id": str(file_doc.id)},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_INVALID_TYPE"


async def test_job_poll_denied_for_non_owner(client, api_key, other_api_key, blank_image_bytes):
    upload_resp = await client.post(
        "/v1/image/upload",
        files={"file": ("owned-by-a.png", blank_image_bytes, "image/png")},
        headers={"X-API-Key": api_key["key"]},
    )
    file_id = upload_resp.json()["data"]["file_id"]
    ocr_resp = await client.post(
        "/v1/image/ocr", json={"file_id": file_id}, headers={"X-API-Key": api_key["key"]}
    )
    job_id = ocr_resp.json()["data"]["job_id"]

    resp = await client.get(f"/v1/jobs/{job_id}", headers={"X-API-Key": other_api_key["key"]})
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "JOB_FORBIDDEN"


async def test_image_upload_rejects_unsupported_extension(client, api_key):
    """Tier 1 `/image/upload` validates against `IMAGE_ALLOWED_EXTENSIONS`
    (`.jpg`/`.jpeg`/`.png`) via the same shared `save_uploaded_file` path
    `/files/upload` and `/pdf/upload` use, just scoped to a different
    allow-list."""
    resp = await client.post(
        "/v1/image/upload",
        files={"file": ("notes.txt", b"not an image", "text/plain")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "FILE_INVALID_TYPE"
