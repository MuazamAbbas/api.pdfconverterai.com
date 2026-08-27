"""Per-API-key daily usage cap for `ai_tools`'s OpenRouter-backed endpoints
(ADR-018 cost/abuse protection; concrete numbers from the approved
feature-spec, docs/roadmap/SPRINT_STATUS.md 2026-08-27 entry "feature-spec
approved: Keyword Research via OpenRouter").

Deliberately separate from `app.core.security.verify_api_key`'s existing
`rate_limit_per_day`/`usage_count` check - that is a per-key, all-endpoints
quota already enforced on every authenticated request
(`app/core/security.py`). This module is a narrower sub-budget scoped just
to `ai_tools`'s OpenRouter-backed endpoints: Keyword Research today, and
(per ADR-018) Social Trend Analyzer/SEO Audit/Content Idea Generator later.
Named/scoped generically (`check_and_increment_ai_tools_daily_usage`, not
`keyword_research`-specific) so those later tools share the same daily
budget/collection instead of each getting their own cap.

Backed by a new `ai_tools_usage` Mongo collection - flagged here per
CLAUDE.md's "don't invent a new collection without flagging it" rule: this
is not one of the six named collections
(`users, files, jobs, tool_history, audit_logs, system_settings`). It is a
narrow, low-risk addition (one doc per `{apiKeyId, date}`, a single
integer counter, no PII beyond the already-existing API key id) - flagged
for `database-agent`/an ADR review rather than treated as pre-approved.

One document per `{apiKeyId, date}`, upserted with `$inc`, mirroring the
`usage_count` counter pattern in `app/core/security.py` and using the same
Mongo-backed convention (`app.core.database.db`).
"""
import logging
from datetime import date, datetime, timedelta

from pymongo import ReturnDocument
from pymongo.errors import DuplicateKeyError

from app.core.database import db

logger = logging.getLogger(__name__)

# ADR-018 / approved feature-spec concrete number: 20 ai_tools
# OpenRouter-backed-endpoint requests/day per API key.
AI_TOOLS_DAILY_LIMIT = 20

# Counter docs only need to exist long enough to answer "today's count" -
# a generous multi-day retention (rather than expiring right after the day
# in question) keeps a short window of usage history around for support/
# debugging without accumulating unboundedly, mirroring the TTL convention
# `app/core/database.py` already applies to `files`/`jobs` (an `expiresAt`
# field + a `expireAfterSeconds=0` TTL index on it).
_RETENTION_DAYS = 35


async def check_and_increment_ai_tools_daily_usage(api_key_id: str) -> bool:
    """Atomically increments today's `ai_tools` OpenRouter-backed-endpoint
    usage counter for `api_key_id` and reports whether the request is
    within `AI_TOOLS_DAILY_LIMIT`.

    Args:
        api_key_id: the API key's Mongo `_id`, as a string.

    Returns:
        True if the request is allowed (today's count, including this
        request, is <= `AI_TOOLS_DAILY_LIMIT`). False if the cap was
        already reached - the counter is still incremented in that case
        (matches the existing `usage_count` pattern in
        `app/core/security.py`, which also counts the request that hits
        the limit), so the caller must reject the request rather than
        proceed.

    Callers (e.g. `POST /ai_tools/keyword_research`) must call this
    *before* creating the job, and on `False` reject with:
    `raise api_error(429, "Daily AI tools request limit reached, try again
    tomorrow", "AI_TOOLS_DAILY_LIMIT_EXCEEDED")`.
    """
    today = date.today().isoformat()
    query = {"apiKeyId": str(api_key_id), "date": today}
    update = {
        "$inc": {"count": 1},
        "$setOnInsert": {
            "createdAt": datetime.utcnow(),
            "expiresAt": datetime.utcnow() + timedelta(days=_RETENTION_DAYS),
        },
    }
    try:
        result = await db.ai_tools_usage.find_one_and_update(
            query, update, upsert=True, return_document=ReturnDocument.AFTER
        )
    except DuplicateKeyError:
        # Two concurrent first-requests-of-the-day for the same key can both
        # attempt the upsert-insert before either commits, and only one can
        # win the unique {apiKeyId, date} index - the loser retries as a
        # plain update against the now-existing doc rather than surfacing an
        # unhandled 500 (fail-closed either way: this can only ever cost the
        # loser an extra increment, never let a request through uncounted).
        result = await db.ai_tools_usage.find_one_and_update(
            query, update, upsert=True, return_document=ReturnDocument.AFTER
        )
    count = result["count"] if result else 1
    allowed = count <= AI_TOOLS_DAILY_LIMIT
    if not allowed:
        logger.warning(
            "🚫 ai_tools daily usage cap reached for key=%s (count=%d, limit=%d)",
            api_key_id,
            count,
            AI_TOOLS_DAILY_LIMIT,
        )
    return allowed
