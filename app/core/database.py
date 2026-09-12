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
        # `content` module (ADR-021 foundation, Tools Metadata CMS feature
        # spec approved 2026-09-04) - `content_tool_metadata` is a new
        # collection, same flagging convention as `content_categories`/
        # `tags` above. Full indexing/uniqueness reasoning lives in
        # app/schemas/content_tool_metadata.py's module docstring; summary:
        #   - `slug` unique - the primary lookup key for
        #     GET /v1/content/tool-metadata/{slug}, and doubles as the
        #     DB-layer backstop against a duplicate-slug create ever
        #     succeeding (same insurance role content_categories_slug_unique/
        #     tags_slug_unique play for their own collections).
        #   - No `category` index: at most ~57 documents will ever exist
        #     (one per tools-registry.ts tool), and no planned query filters
        #     this collection by category - the public read path is always a
        #     single find_one({"slug": ...}) by the unique key above. Same
        #     "tiny collection, index only what's actually queried"
        #     reasoning as content_categories_order/homepage_sections_order.
        # No TTL index: structural per-tool marketing content, no natural
        # expiry.
        await db.content_tool_metadata.create_index(
            "slug", unique=True, name="content_tool_metadata_slug_unique"
        )
        # `content` module (ADR-021 foundation, Blog/News CMS - direct
        # extension of the just-shipped Tools Metadata CMS above) -
        # `content_blog_posts` is a new collection, same flagging convention
        # as `content_categories`/`tags`/`content_tool_metadata` above. Full
        # indexing/uniqueness reasoning lives in
        # app/schemas/content_blog_post.py's module docstring; summary:
        #   - `slug` unique - the primary lookup key for
        #     GET /v1/content/blog-posts/{slug}, and doubles as the DB-layer
        #     backstop against a duplicate-slug create ever succeeding (same
        #     insurance role content_categories_slug_unique/tags_slug_unique/
        #     content_tool_metadata_slug_unique play for their own
        #     collections).
        #   - `(status, published_at)` compound index, DESCENDING on
        #     published_at: unlike every sibling collection in this feature
        #     family (all bounded - content_categories ~10-20 rows,
        #     content_tool_metadata capped at the tool count ~57), this is
        #     the first collection here expected to grow unboundedly over
        #     the site's editorial lifetime. It directly serves the public
        #     blog index's real query shape -
        #     find({"status": "published"}).sort("published_at", -1) with
        #     pagination - rather than relying on the "tiny collection, full
        #     scan is sub-millisecond" reasoning the siblings correctly use.
        #     This is a deliberate, proactive divergence from this feature
        #     family's usual "index only what's actually queried, skip it on
        #     small collections" default - see the schema docstring for the
        #     full contrast against content_tool_metadata's opposite call.
        # No TTL index: structural editorial content, no natural expiry -
        # same as every sibling collection in this feature family.
        await db.content_blog_posts.create_index(
            "slug", unique=True, name="content_blog_posts_slug_unique"
        )
        await db.content_blog_posts.create_index(
            [("status", 1), ("published_at", -1)],
            name="content_blog_posts_status_published_at",
        )
        # `content` module (ADR-021 foundation, ADR-022 - Dynamic Pages
        # builder, task #48) - `content_pages` is a new collection, same
        # flagging convention as `content_categories`/`tags`/
        # `content_tool_metadata`/`content_blog_posts` above. Full
        # indexing/uniqueness reasoning lives in
        # app/schemas/content_page.py's module docstring; summary:
        #   - `slug` unique - the primary lookup key for
        #     GET /v1/content/pages/{slug}, and doubles as the DB-layer
        #     backstop against a duplicate-slug create ever succeeding (same
        #     insurance role content_categories_slug_unique/tags_slug_unique/
        #     content_tool_metadata_slug_unique/
        #     content_blog_posts_slug_unique play for their own
        #     collections).
        #   - Deliberately NO `(status, published_at)` compound index, unlike
        #     content_blog_posts: the public read path here is a single
        #     find_one({"slug": ..., "status": "published"}) already served
        #     by the slug unique index (a point lookup - the status check on
        #     the one fetched document is free), and nothing in the approved
        #     spec has the admin list route filtering/sorting by status or
        #     published_at at all (it lists ALL pages, drafts included, full
        #     stop) - there is no sorted/filtered/paginated query this
        #     collection actually serves that such an index would help,
        #     unlike content_blog_posts's real
        #     find({"status": "published"}).sort("published_at", -1)
        #     pagination query. Closer to content_tool_metadata's "tiny,
        #     bounded collection, index only what's actually queried"
        #     reasoning than to content_blog_posts's proactive case - see the
        #     schema docstring for the honest caveat on why this assumption
        #     could stop holding (no hard cap enforced anywhere on this
        #     collection, unlike content_tool_metadata's natural cap at the
        #     tool count).
        # No TTL index: structural editorial content, no natural expiry -
        # same as every sibling collection in this feature family.
        await db.content_pages.create_index(
            "slug", unique=True, name="content_pages_slug_unique"
        )
        logger.info("Verified files/jobs indexes")
    except Exception as e:
        logger.error(f"Failed to create files/jobs indexes: {str(e)}")
        raise