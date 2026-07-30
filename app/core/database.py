import logging
from urllib.parse import urlsplit, urlunsplit

from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings

logger = logging.getLogger(__name__)


def _mask_connection_string(url: str) -> str:
    """Redact the password from a Mongo connection string before logging it.
    Unlike API keys (last-4 convention), a DB credential gets fully masked -
    there's no legitimate debugging value in a partial password."""
    try:
        parts = urlsplit(url)
        netloc = parts.netloc
        if parts.password:
            netloc = netloc.replace(f":{parts.password}@", ":***@")
        return urlunsplit((parts.scheme, netloc, parts.path, parts.query, parts.fragment))
    except Exception:
        return "<unparseable>"


try:
    logger.debug("Connecting to MongoDB with URL: %s", _mask_connection_string(settings.database_url))
    client = AsyncIOMotorClient(settings.database_url)
    db = client["pdfconverterai"]
except Exception as e:
    logger.error(f"Failed to connect to MongoDB: {str(e)}")
    raise

async def get_db():
    try:
        yield db
    except Exception as e:
        logger.error(f"Error in get_db: {str(e)}")
        raise
    finally:
        pass  # Motor handles connection cleanup


async def ensure_indexes():
    """Create (or verify) indexes for collections that need them.

    Mongo's create_index is idempotent for an equivalent definition, so
    this is safe to run on every app startup rather than requiring a
    separate migration step. Covers the files/jobs metadata lifecycle
    (Handbook Part C.9, ADR-007): TTL indexes keep Mongo cleanup in sync
    with the filesystem worker, and the jobs.fileId/jobs.status indexes
    back the queries the worker and GET /jobs/{id} will run.
    """
    try:
        await db.files.create_index("expiresAt", expireAfterSeconds=0, name="files_expiresAt_ttl")
        await db.jobs.create_index("expiresAt", expireAfterSeconds=0, name="jobs_expiresAt_ttl")
        await db.jobs.create_index("fileId", name="jobs_fileId")
        await db.jobs.create_index("status", name="jobs_status")
        logger.info("Verified files/jobs indexes")
    except Exception as e:
        logger.error(f"Failed to create files/jobs indexes: {str(e)}")
        raise