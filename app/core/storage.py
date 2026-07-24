"""Low-level on-disk storage for uploaded/generated files (Handbook Part C.1, C.9).

`app/services/files/service.py` owns the Mongo-tracked read/write API
(that's where `files` documents get created); this module only owns the
constant both it and the worker share (`STORAGE_PATH`) plus the on-disk
retention sweep - the TTL index on `files.expiresAt` (see
`app/core/database.py::ensure_indexes`) expires the *Mongo record*
automatically, but something still has to delete the actual bytes from
disk, which is what `cleanup_expired_files()` does. Registered on an
APScheduler interval job from `app/main.py`'s startup event (same pattern
this module used before, just now driven by the `files` collection instead
of raw filesystem mtimes only).
"""
import logging
import os
from datetime import datetime, timedelta

from app.core.config import settings
from app.core.database import db

logger = logging.getLogger(__name__)

STORAGE_PATH = "/tmp/pdfconverterai"
os.makedirs(STORAGE_PATH, exist_ok=True)


def _delete_path(path: str | None) -> None:
    if not path:
        return
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.debug("Deleted expired file: %s", path)
    except OSError as e:
        logger.error("Failed to delete %s: %s", path, e)


async def cleanup_expired_files() -> None:
    """Delete disk bytes for `files` docs already past `expiresAt`, then remove
    the Mongo doc too (idempotent - safe even if the TTL index already did it).

    Also sweeps `STORAGE_PATH` by file mtime as a fallback, in case a disk
    file's Mongo doc was already TTL-deleted before this ran, or an insert
    failed after a successful disk write - so nothing is orphaned on disk
    past the retention window even if it's untracked in Mongo.
    """
    now = datetime.utcnow()
    expired_count = 0
    async for doc in db.files.find({"expiresAt": {"$lte": now}}):
        _delete_path(doc.get("storagePath"))
        await db.files.delete_one({"_id": doc["_id"]})
        expired_count += 1

    cutoff = now - timedelta(minutes=settings.file_retention_minutes)
    orphan_count = 0
    try:
        for name in os.listdir(STORAGE_PATH):
            path = os.path.join(STORAGE_PATH, name)
            try:
                mtime = datetime.utcfromtimestamp(os.path.getmtime(path))
            except OSError:
                continue
            if mtime < cutoff:
                _delete_path(path)
                orphan_count += 1
    except OSError as e:
        logger.error("Failed to sweep %s: %s", STORAGE_PATH, e)

    if expired_count or orphan_count:
        logger.info(
            "Cleanup swept %d expired files.* docs and %d orphaned disk files",
            expired_count,
            orphan_count,
        )
