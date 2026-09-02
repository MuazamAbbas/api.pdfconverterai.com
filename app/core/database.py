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
        # ADR-018 / app/services/ai/usage_limits.py: one document per
        # {apiKeyId, date}, unique so concurrent upserts for the same
        # key/day can't create duplicate counter docs (which would break
        # the daily-cap check's atomicity).
        await db.ai_tools_usage.create_index(
            [("apiKeyId", 1), ("date", 1)], unique=True, name="ai_tools_usage_apiKeyId_date"
        )
        # Same TTL convention as files/jobs above - counter docs are only
        # useful for a short support/debugging window, not indefinitely.
        await db.ai_tools_usage.create_index(
            "expiresAt", expireAfterSeconds=0, name="ai_tools_usage_expiresAt_ttl"
        )
        # Feature-spec approved 2026-08-31 (SEO Audit) /
        # app/services/seo/usage_limits.py: one document per
        # {apiKeyId, hourBucket}, unique so concurrent upserts for the same
        # key/hour can't create duplicate counter docs (which would break
        # the hourly-cap check's atomicity). New collection - flagged for
        # database-agent review per CLAUDE.md's "don't invent a new
        # collection without flagging it" rule, same convention
        # `ai_tools_usage` above was flagged under.
        await db.seo_tools_usage.create_index(
            [("apiKeyId", 1), ("hourBucket", 1)], unique=True, name="seo_tools_usage_apiKeyId_hourBucket"
        )
        # Same TTL convention as files/jobs/ai_tools_usage above.
        await db.seo_tools_usage.create_index(
            "expiresAt", expireAfterSeconds=0, name="seo_tools_usage_expiresAt_ttl"
        )
        # `auth` module (ADR-019 pending) - `admin_users` is a new collection,
        # flagged per CLAUDE.md's "don't invent a new collection without
        # flagging it" rule. Unique on email so two concurrent
        # `scripts/seed_admin.py` runs for the same address can't create
        # duplicate documents (app/services/auth/admin_user_service.py's
        # own find-before-insert check is a TOCTOU-vulnerable first line of
        # defense only; this index is the actual guarantee).
        await db.admin_users.create_index("email", unique=True, name="admin_users_email_unique")
        logger.info("Verified files/jobs indexes")
    except Exception as e:
        logger.error(f"Failed to create files/jobs indexes: {str(e)}")
        raise