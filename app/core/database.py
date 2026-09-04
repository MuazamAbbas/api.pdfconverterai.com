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
        # `admin` module (ADR-019) - `homepage_sections` is a new collection,
        # flagged per CLAUDE.md's "don't invent a new collection without
        # flagging it" rule, same convention as `admin_users` above. Full
        # indexing/uniqueness reasoning lives in
        # app/schemas/homepage_section.py's module docstring; summary:
        #   - `order` indexed (non-unique) - backs both the public read
        #     path (`find({enabled: True}).sort("order")`) and the admin
        #     list path (`find({}).sort("order")`). NOT unique: the bulk
        #     reorder endpoint can transiently write colliding `order`
        #     values mid-batch, which a unique index would reject.
        #   - `type` gets a *partial* unique index scoped to
        #     `type: "tool_grid"` only, so MongoDB guarantees at most one
        #     tool_grid document can ever exist (the homepage's structural
        #     section) - a DB-layer backstop for the route layer's
        #     DELETE-rejection logic, not a substitute for it.
        # No TTL index: homepage sections have no natural expiry (this is
        # structural site config, not transient processing/upload metadata).
        await db.homepage_sections.create_index("order", name="homepage_sections_order")
        await db.homepage_sections.create_index(
            "type",
            unique=True,
            partialFilterExpression={"type": "tool_grid"},
            name="homepage_sections_type_tool_grid_unique",
        )
        # `auth` module (ADR-020) - `users` is a new collection, flagged per
        # CLAUDE.md's "don't invent a new collection without flagging it"
        # rule, same convention as `admin_users` above. Deliberately
        # structurally separate from `admin_users` (ADR-020's isolation
        # table) - never share, merge, or cross-query these two
        # collections. Unique on email so two concurrent
        # `POST /auth/signup` requests for the same address can't create
        # duplicate documents (app/schemas/user.py's own
        # find-before-insert check, once backend-builder adds it, is a
        # TOCTOU-vulnerable first line of defense only; this index is the
        # actual guarantee) - same reasoning as admin_users_email_unique.
        await db.users.create_index("email", unique=True, name="users_email_unique")
        # Deliberately NOT a TTL index. `users` documents have no natural
        # expiry (an account should never be auto-deleted), unlike
        # files/jobs/*_usage above. In particular, `password_reset_expires_at`
        # is NOT TTL-indexed even though it looks like an expiry field: a
        # TTL index deletes the whole matched document once the indexed
        # date passes, which here would mean a forgotten/abandoned
        # password-reset request silently deletes the user's entire
        # account once the reset link expires. Expiry for that field is
        # instead enforced at the application layer (reject if
        # `password_reset_expires_at < now` when validating the token) -
        # see app/schemas/user.py's docstring.
        # `content` module (ADR-021) - `content_categories` is a new
        # collection, flagged per CLAUDE.md's "don't invent a new
        # collection without flagging it" rule, same convention as
        # `admin_users`/`homepage_sections` above. Full indexing/uniqueness
        # reasoning lives in app/schemas/content_category.py's module
        # docstring; summary:
        #   - `slug` unique across BOTH content_type values (not partial) -
        #     doubles as the DB-layer backstop against a duplicate
        #     tool_metadata row ever being inserted twice (e.g. a re-run of
        #     seed_content_categories.py), the same insurance role
        #     homepage_sections_type_tool_grid_unique plays for tool_grid.
        #   - `order` indexed (non-unique) - backs the sorted list queries
        #     for both content types. NOT compounded with `content_type`,
        #     same reasoning as homepage_sections_order (tiny collection,
        #     full scan already sub-millisecond).
        #   - No DB-layer backstop exists for the edit/delete read-only
        #     constraint on tool_metadata rows themselves (MongoDB Community
        #     has no per-document conditional write-block mechanism) - that
        #     invariant is application-layer only, enforced in
        #     app/services/content/categories_service.py.
        # No TTL index: structural taxonomy data, no natural expiry.
        await db.content_categories.create_index(
            "slug", unique=True, name="content_categories_slug_unique"
        )
        await db.content_categories.create_index("order", name="content_categories_order")
        # `content` module (ADR-021) - `tags` is a new collection, same
        # flagging convention as `content_categories` above. Unique on
        # `slug` so two concurrent `get_or_create_tag` calls normalizing the
        # same raw string (e.g. "SEO" and "seo") can't create duplicate
        # documents (app/services/content/tags_service.py's own
        # find-before-upsert check is a TOCTOU-vulnerable first line of
        # defense only; this index is the actual guarantee) - same
        # reasoning as admin_users_email_unique/users_email_unique. No TTL
        # index: tags are not transient processing/upload metadata and have
        # no natural expiry.
        await db.tags.create_index("slug", unique=True, name="tags_slug_unique")
        logger.info("Verified files/jobs indexes")
    except Exception as e:
        logger.error(f"Failed to create files/jobs indexes: {str(e)}")
        raise