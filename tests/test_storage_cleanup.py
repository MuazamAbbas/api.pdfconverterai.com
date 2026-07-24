"""`app.core.storage.cleanup_expired_files()` (Handbook Part C.1/C.9):
expired `files` docs must lose both their Mongo record and their on-disk
bytes; untracked orphan files past the retention window on disk must also
be swept even without a Mongo doc.
"""
import hashlib
import os
from datetime import datetime, timedelta

import pytest

from app.core.database import db
from app.core.storage import STORAGE_PATH, cleanup_expired_files
from app.schemas.file import FileCreate

# See tests/test_worker_retry.py's module comment for why this is pinned to
# the session-scoped loop (Motor's shared `app.core.database.db` client).
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_expired_file_doc_removes_both_mongo_record_and_disk_bytes(api_key):
    os.makedirs(STORAGE_PATH, exist_ok=True)
    content = b"expired file content"
    path = os.path.join(STORAGE_PATH, "expired-cleanup-test.pdf")
    with open(path, "wb") as f:
        f.write(content)

    file_create = FileCreate(
        storagePath=path,
        checksum=hashlib.sha256(content).hexdigest(),
        originalFilename="expired-cleanup-test.pdf",
        sizeBytes=len(content),
        mimeType="application/pdf",
        ownerApiKeyId=api_key["id"],
        expiresAt=datetime.utcnow() - timedelta(minutes=5),  # already expired
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))

    assert os.path.exists(path)

    await cleanup_expired_files()

    assert not os.path.exists(path), "expired file's bytes must be deleted from disk"
    doc = await db.files.find_one({"_id": insert_result.inserted_id})
    assert doc is None, "expired file's Mongo doc must be removed"


async def test_non_expired_file_doc_is_left_alone(api_key):
    os.makedirs(STORAGE_PATH, exist_ok=True)
    content = b"still valid content"
    path = os.path.join(STORAGE_PATH, "not-expired-cleanup-test.pdf")
    with open(path, "wb") as f:
        f.write(content)

    file_create = FileCreate(
        storagePath=path,
        checksum=hashlib.sha256(content).hexdigest(),
        originalFilename="not-expired-cleanup-test.pdf",
        sizeBytes=len(content),
        mimeType="application/pdf",
        ownerApiKeyId=api_key["id"],
        expiresAt=datetime.utcnow() + timedelta(minutes=30),  # not expired
    )
    insert_result = await db.files.insert_one(file_create.model_dump(by_alias=True))

    try:
        await cleanup_expired_files()

        assert os.path.exists(path), "non-expired file's bytes must survive cleanup"
        doc = await db.files.find_one({"_id": insert_result.inserted_id})
        assert doc is not None, "non-expired file's Mongo doc must survive cleanup"
    finally:
        if os.path.exists(path):
            os.remove(path)
        await db.files.delete_one({"_id": insert_result.inserted_id})


async def test_orphaned_disk_file_past_retention_window_is_swept_even_without_mongo_doc():
    """Fallback path: a file on disk with no (or already-deleted) Mongo
    record, old enough to be past the retention window by mtime alone."""
    os.makedirs(STORAGE_PATH, exist_ok=True)
    path = os.path.join(STORAGE_PATH, "orphan-cleanup-test.bin")
    with open(path, "wb") as f:
        f.write(b"orphaned bytes, no mongo doc")

    # Backdate mtime well past the retention window so the orphan sweep
    # (mtime-based fallback) picks it up regardless of file_retention_minutes.
    old_time = (datetime.utcnow() - timedelta(hours=2)).timestamp()
    os.utime(path, (old_time, old_time))

    await cleanup_expired_files()

    assert not os.path.exists(path), "orphaned disk file past retention must be swept"
