"""ADR-016 ownership-check regression coverage for `GET /v1/jobs/{id}`
(app/routers/jobs.py, docs/architecture/adr/ADR-016-jobs-ownership-check-ttl-gap.md).

Before ADR-016, ownership was derived transitively from the linked `files`
document (`file_doc.ownerApiKeyId`) - so once that File record TTL-expired
(files.py's own retention window fires *before* a job's own equally-short
but independently-ticking `expiresAt`), the ownership check fail-open'd
(`if file_doc is not None and ...` never even ran when the file was gone)
and any valid `jobs`-scoped API key could read another tenant's job
status/result/error.

The fix stamps `ownerApiKeyId` directly on the job at creation time
(`app/services/jobs/service.py::create_job`/`create_multi_file_job`) and
`GET /jobs/{id}` now checks that field first - authoritatively, with no
dependency on the File record at all - and only falls back to the old
file-derived check for legacy (pre-fix) job docs that have no
`ownerApiKeyId` key at all.

This module exercises all three ownership-check paths directly against the
real `jobs`/`files` routers, real local Mongo (matching
`tests/test_files_jobs_pdf_flow.py`/`tests/test_worker_retry.py`'s existing
conventions, not mocks):

1. The new authoritative `ownerApiKeyId` path, with the linked File record
   never created at all (the exact fail-open repro from the ADR) - both the
   deny case (non-owning key) and the allow case (owning key).
2. The legacy fallback path (`ownerApiKeyId` absent from the doc, simulating
   a pre-fix job) - both its deny and allow cases - so the fix doesn't
   regress that transitional window before such docs self-expire.
"""
from datetime import datetime

import pytest
from bson import ObjectId

from app.core.database import db
from app.schemas.common import default_expiry
from app.schemas.job import JobDocument
from app.services.jobs.service import create_job

# Same reasoning as tests/test_worker_retry.py's module docstring: Motor's
# shared `app.core.database.db` client is bound to a single event loop for
# the whole session (see tests/conftest.py's
# `asyncio_default_fixture_loop_scope = "session"`), so these tests are
# pinned to that same session-scoped loop.
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def _insert_legacy_job_doc(file_id: ObjectId, job_type: str = "pdf_convert") -> JobDocument:
    """Inserts a `jobs` document shaped exactly like a pre-ADR-016 doc: no
    `ownerApiKeyId` key present at all (not even `null`) - reproducing a
    real job document written before this fix landed, for the fallback
    path's own test coverage."""
    raw = {
        "fileId": file_id,
        "fileIds": None,
        "type": job_type,
        "status": "pending",
        "result": None,
        "error": None,
        "retryCount": 0,
        "maxRetries": 3,
        "createdAt": datetime.utcnow(),
        "updatedAt": datetime.utcnow(),
        "expiresAt": default_expiry(),
        # Deliberately no "ownerApiKeyId" key - see docstring above.
    }
    insert_result = await db.jobs.insert_one(raw)
    doc = await db.jobs.find_one({"_id": insert_result.inserted_id})
    return JobDocument(**doc)


async def test_new_owned_job_forbidden_for_non_owner_even_with_file_gone(
    client, api_key, other_api_key
):
    """The exact ADR-016 fail-open repro: job stamped with owner A's key at
    creation time, and the linked File record never created at all
    (simulating a TTL-expired/gone File doc). A different key (B) polling it
    must still get 403 - the fix checks `job.ownerApiKeyId` first and no
    longer needs the File record to exist at all to deny access. Under the
    pre-fix code, `get_file_by_id` would have returned `None` here and the
    `if file_doc is not None and ...` check would have silently skipped,
    fail-opening the read to key B.
    """
    fake_file_id = ObjectId()  # no `files` document ever created for this id
    job = await create_job(fake_file_id, "pdf_convert", api_key["id"])
    try:
        resp = await client.get(
            f"/v1/jobs/{job.id}", headers={"X-API-Key": other_api_key["key"]}
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "JOB_FORBIDDEN"
    finally:
        await db.jobs.delete_one({"_id": job.id})


async def test_new_owned_job_still_pollable_by_owner_with_file_gone(client, api_key):
    """Adjacent legitimate path: the owning key must still be able to poll
    its own job successfully even with the linked File record absent - the
    fix must not regress usability for the normal case just because it no
    longer depends on the File record."""
    fake_file_id = ObjectId()
    job = await create_job(fake_file_id, "pdf_convert", api_key["id"])
    try:
        resp = await client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key["key"]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["job_id"] == str(job.id)
        assert body["data"]["status"] == "pending"
    finally:
        await db.jobs.delete_one({"_id": job.id})


async def test_legacy_job_without_owner_field_falls_back_to_file_check_and_denies(
    client, api_key, other_api_key, corrupt_pdf_bytes
):
    """Legacy pre-ADR-016 doc (`ownerApiKeyId` key absent, not merely
    `None`-valued - matches what a real old doc looks like in Mongo): falls
    back to the file-derived check, which must still deny a non-owning key
    when the linked File record is present and owned by someone else."""
    upload_resp = await client.post(
        "/v1/files/upload",
        files={"file": ("legacy-owned-by-a.pdf", corrupt_pdf_bytes, "application/pdf")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert upload_resp.status_code == 200
    file_id = upload_resp.json()["data"]["file_id"]
    job = await _insert_legacy_job_doc(ObjectId(file_id))
    try:
        resp = await client.get(
            f"/v1/jobs/{job.id}", headers={"X-API-Key": other_api_key["key"]}
        )
        assert resp.status_code == 403
        body = resp.json()
        assert body["success"] is False
        assert body["error"]["code"] == "JOB_FORBIDDEN"
    finally:
        await db.jobs.delete_one({"_id": job.id})


async def test_legacy_job_without_owner_field_falls_back_to_file_check_and_allows_owner(
    client, api_key, corrupt_pdf_bytes
):
    """Same legacy fallback path, allow case: the file's real owner can
    still poll a legacy (pre-ADR-016) job doc successfully."""
    upload_resp = await client.post(
        "/v1/files/upload",
        files={"file": ("legacy-owned-by-a-2.pdf", corrupt_pdf_bytes, "application/pdf")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert upload_resp.status_code == 200
    file_id = upload_resp.json()["data"]["file_id"]
    job = await _insert_legacy_job_doc(ObjectId(file_id))
    try:
        resp = await client.get(f"/v1/jobs/{job.id}", headers={"X-API-Key": api_key["key"]})
        assert resp.status_code == 200
        body = resp.json()
        assert body["success"] is True
        assert body["data"]["job_id"] == str(job.id)
    finally:
        await db.jobs.delete_one({"_id": job.id})
