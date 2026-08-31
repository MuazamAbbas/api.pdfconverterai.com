"""Per-API-key hourly usage cap for `seo_tools`'s `seo_audit` endpoint
(feature-spec approved 2026-08-31, docs/roadmap/SPRINT_STATUS.md "feature-spec
approved: SEO Audit (last v1-parity tool)" entry, acceptance criterion #3).

Deliberately separate from `app.core.security.verify_api_key`'s existing
`rate_limit_per_day`/`usage_count` check - that is a per-key, all-endpoints
quota already enforced on every authenticated request
(`app/core/security.py`). This module is a narrower sub-budget scoped just
to `seo_tools`'s `seo_audit` endpoint, because it carries materially more
abuse/cost surface than this router's other, single-computation endpoints:
it fetches the target page, up to 20 links found on it, and its
`sitemap.xml`, all SSRF-checked but still real outbound concurrent network
I/O per call - a narrow hourly cap (not a daily one, unlike `ai_tools_usage`)
matches that concurrent-load/SSRF-fetch-abuse risk profile specifically,
rather than the per-call LLM-cost risk `ai_tools_usage` exists to bound.

Structural mirror of `app/services/ai/usage_limits.py`'s
`check_and_increment_ai_tools_daily_usage` - same upsert-`$inc` counter
pattern, same TTL/retention convention - keyed `{apiKeyId, hourBucket}`
(a UTC calendar-hour bucket string) instead of `{apiKeyId, date}`.

Backed by a new `seo_tools_usage` Mongo collection - flagged here per
CLAUDE.md's "don't invent a new collection without flagging it" rule, same
as `usage_limits.py`'s own docstring does for `ai_tools_usage`: this is not
one of the six named collections
(`users, files, jobs, tool_history, audit_logs, system_settings`). It is a
narrow, low-risk addition (one doc per `{apiKeyId, hourBucket}`, a single
integer counter, no PII beyond the already-existing API key id) - flagged
for `database-agent` review of the index/TTL setup added to
`app/core/database.py::ensure_indexes` in this same change, rather than
treated as pre-approved.
"""
import logging
from datetime import datetime, timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import db

logger = logging.getLogger(__name__)

# Approved feature-spec concrete number (docs/roadmap/SPRINT_STATUS.md
# 2026-08-31 entry, amendment (2)): 10 seo_audit requests/hour per API key.
SEO_AUDIT_HOURLY_LIMIT = 10

# Counter docs only need to exist long enough to answer "this hour's
# count" - a generous multi-day retention (rather than expiring right
# after the hour in question) keeps a short window of usage history around
# for support/debugging without accumulating unboundedly, mirroring the
# TTL convention `app/core/database.py` already applies to
# `files`/`jobs`/`ai_tools_usage` (an `expiresAt` field + an
# `expireAfterSeconds=0` TTL index on it).
_RETENTION_DAYS = 35


def _current_hour_bucket(now: datetime | None = None) -> str:
    """UTC calendar-hour bucket, e.g. `"2026-08-31T14"` - one counter doc
    per key per UTC hour, not a rolling 60-minute window."""
    return (now or datetime.utcnow()).strftime("%Y-%m-%dT%H")


async def check_and_increment_seo_audit_hourly_usage(api_key_id: str) -> bool:
    """Atomically increments this UTC hour's `seo_audit` usage counter for
    `api_key_id` and reports whether the request is within
    `SEO_AUDIT_HOURLY_LIMIT`.

    Args:
        api_key_id: the API key's Mongo `_id`, as a string.

    Returns:
        True if the request is allowed (this hour's count, including this
        request, is <= `SEO_AUDIT_HOURLY_LIMIT`). False if the cap was
        already reached - the counter is still incremented in that case
        (matches the existing `usage_count` pattern in
        `app/core/security.py` and `check_and_increment_ai_tools_daily_usage`,
        both of which also count the request that hits the limit), so the
        caller must reject the request rather than proceed.

    Callers (`POST /seo_tools/seo_audit`) must call this *before* starting
    the audit, and on `False` reject with:
    `raise api_error(429, "Hourly SEO Audit request limit reached, try
    again later", "SEO_AUDIT_RATE_LIMIT_EXCEEDED")`.
    """
    hour_bucket = _current_hour_bucket()
    query = {"apiKeyId": str(api_key_id), "hourBucket": hour_bucket}
    update = {
        "$inc": {"count": 1},
        "$setOnInsert": {
            "createdAt": datetime.utcnow(),
            "expiresAt": datetime.utcnow() + timedelta(days=_RETENTION_DAYS),
        },
    }
    try:
        result = await db.seo_tools_usage.find_one_and_update(
            query, update, upsert=True, return_document=ReturnDocument.AFTER
        )
    except DuplicateKeyError:
        # Two concurrent first-requests-of-the-hour for the same key can
        # both attempt the upsert-insert before either commits, and only
        # one can win the unique {apiKeyId, hourBucket} index - the loser
        # retries as a plain update against the now-existing doc rather
        # than surfacing an unhandled 500 (fail-closed either way: this can
        # only ever cost the loser an extra increment, never let a request
        # through uncounted).
        result = await db.seo_tools_usage.find_one_and_update(
            query, update, upsert=True, return_document=ReturnDocument.AFTER
        )
    count = result["count"] if result else 1
    allowed = count <= SEO_AUDIT_HOURLY_LIMIT
    if not allowed:
        logger.warning(
            "🚫 seo_audit hourly usage cap reached for key=%s (count=%d, limit=%d)",
            api_key_id,
            count,
            SEO_AUDIT_HOURLY_LIMIT,
        )
    return allowed
