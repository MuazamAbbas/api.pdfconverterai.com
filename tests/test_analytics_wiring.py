"""Proof-of-pattern wiring tests for the `analytics` module (ADR-023): one
existing Tier 1 tool (`POST /web_tools/url_encode`) and one existing Tier 2
job's transition-to-Completed path (`pdf_split`) each call
`app.analytics.service.record_tool_usage` exactly once per successful
run/completion (SPRINT_STATUS.md's 2026-09-15 analytics module entry, AC3).

Not a retrofit of every tool - see that module's own worker.py/web_tools.py
comments for why only these two were chosen.
"""
import os
from datetime import datetime, timezone
from io import BytesIO

import PyPDF2
import pytest
import pytest_asyncio

import app.worker as worker
from app.core.database import db, ensure_indexes
from app.core.storage import STORAGE_PATH
from app.schemas.job import JobStatus
from app.services.jobs.service import create_job, get_job

pytestmark = pytest.mark.asyncio(loop_scope="session")


@pytest_asyncio.fixture(scope="session", autouse=True)
async def _ensure_analytics_indexes():
    # Idempotent - safe even if tests/test_analytics.py's own copy of this
    # fixture already ran in the same session.
    await ensure_indexes()
    yield


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


async def _get_counter(target: str) -> dict | None:
    return await db.analytics_counters.find_one(
        {"metric_type": "tool_usage", "target": target, "date": _today()}
    )


async def _delete_counter(target: str) -> None:
    await db.analytics_counters.delete_one(
        {"metric_type": "tool_usage", "target": target, "date": _today()}
    )


async def test_url_encode_records_tool_usage(client, api_key):
    await _delete_counter("url-encoder-decoder")
    try:
        resp = await client.post(
            "/v1/web_tools/url_encode",
            json={"url": "https://example.com/a b"},
            headers={"X-API-Key": api_key["key"]},
        )
        assert resp.status_code == 200

        doc = await _get_counter("url-encoder-decoder")
        assert doc is not None
        assert doc["count"] == 1
    finally:
        await _delete_counter("url-encoder-decoder")


def _multi_page_pdf_bytes(num_pages: int) -> bytes:
    writer = PyPDF2.PdfWriter()
    for _ in range(num_pages):
        writer.add_blank_page(width=200, height=200)
    buf = BytesIO()
    writer.write(buf)
    return buf.getvalue()


async def _make_pdf_file_doc(owner_id, content: bytes, filename: str):
    import hashlib

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
        mimeType="application/pdf",
        ownerApiKeyId=owner_id,
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    return FileDocument(**doc)


async def test_pdf_split_completion_records_tool_usage(api_key):
    await _delete_counter("pdf-splitter")
    try:
        file_doc = await _make_pdf_file_doc(
            api_key["id"], _multi_page_pdf_bytes(3), "split-analytics.pdf"
        )
        job = await create_job(file_doc.id, "pdf_split", api_key["id"], params={"ranges": "1-2"})

        await worker.pdf_split({"job_try": 1}, str(job.id))

        updated = await get_job(str(job.id))
        assert updated.status == JobStatus.COMPLETED

        doc = await _get_counter("pdf-splitter")
        assert doc is not None
        assert doc["count"] == 1

        output_doc_id = updated.result["outputFileId"]
        from app.services.files.service import get_file_by_id

        output_doc = await get_file_by_id(output_doc_id)
        if output_doc is not None and os.path.exists(output_doc.storagePath):
            os.remove(output_doc.storagePath)
    finally:
        await _delete_counter("pdf-splitter")


async def test_pdf_split_failure_does_not_record_tool_usage(api_key):
    """A job that never reaches Completed must not increment the counter -
    proves `on_completed` only fires on the success path, not on every
    `_run_job` invocation."""
    await _delete_counter("pdf-splitter")
    try:
        from bson import ObjectId

        job = await create_job(
            ObjectId("0123456789ab0123456789ab"), "pdf_split", api_key["id"], params={"ranges": "1-1"}
        )
        await worker.pdf_split({"job_try": 1}, str(job.id))

        updated = await get_job(str(job.id))
        assert updated.status == JobStatus.FAILED

        doc = await _get_counter("pdf-splitter")
        assert doc is None
    finally:
        await _delete_counter("pdf-splitter")
