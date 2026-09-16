"""`analytics` module service layer (ADR-023: Analytics Module Foundation -
Daily Counter Aggregation, Async In-Process Writes; founder-approved spec,
`docs/roadmap/SPRINT_STATUS.md`'s 2026-09-15 "Spec approved: Analytics
module (foundation)" entry).

Owns every write against `db.analytics_counters` (Handbook Part C.3 - "one
module = one responsibility"), matching the schema/index decisions already
committed in `app/schemas/analytics_counter.py` /
`app/core/database.py::ensure_indexes`. Read-side/dashboard consumption is
explicitly out of scope for this round (see the approved spec) - these are
write-only helpers.

**Core architectural requirement (do not weaken this when adding new call
sites):** analytics must never add latency or failure risk to the
latency-sensitive, AdSense-revenue tool-serving/job-completion path.
`record_tool_usage`/`record_page_view` therefore never raise - every
exception (bad Mongo connection, unexpected input, whatever) is caught and
logged right here, not left for the caller to handle. This is what actually
makes them safe to use as a FastAPI `BackgroundTask` target (Tier 1 HTTP
routes - see `app/routers/analytics.py` and `app/routers/web_tools.py`'s
`url_encode`) or to `await` directly from the ARQ worker process (Tier 2 job
completion - see `app/worker.py`'s `pdf_split`), which has no HTTP response
to attach a `BackgroundTask` to in the first place.

`record_tool_usage`/`record_page_view` were intentionally the *only* two
public entry points here for Round 1 (write-only) - `get_summary` is the
Round 2 read-side addition (2026-09-16 "Spec approved: Admin Dashboard
analytics visualization (graphs)"), the dashboard consumption that Round 1
explicitly deferred. It's a plain query (no upsert, no "never raises"
contract - a read failure should surface as a real error to its admin-only
caller, not be swallowed), but it keeps the same discipline these two
functions already established: no direct `db.analytics_counters` access
from any router/worker/processor, same discipline
`app/services/jobs/service.py` documents for `db.jobs`. Three public entry
points now, not two.
"""
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.database import db
from app.schemas.analytics_counter import MetricType

logger = logging.getLogger(__name__)


async def _increment_counter(metric_type: MetricType, target: str) -> None:
    """Upsert one `analytics_counters` document for
    `(metric_type, target, <today's UTC date>)`, incrementing `count` by 1.

    `created_at` only goes in `$setOnInsert` (first write only); `updated_at`
    is set on *every* write via `$set` so it actually tracks the most recent
    increment - see `app/schemas/analytics_counter.py`'s module docstring
    for the exact gotcha this avoids (the illustrative snippet in the
    approved spec omitted the `updated_at` `$set`, don't copy that omission).

    Never raises - see this module's docstring for why that's the whole
    point.
    """
    try:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        now = datetime.utcnow()
        await db.analytics_counters.update_one(
            {"metric_type": metric_type.value, "target": target, "date": today},
            {
                "$inc": {"count": 1},
                "$setOnInsert": {"created_at": now},
                "$set": {"updated_at": now},
            },
            upsert=True,
        )
        logger.debug(
            "Recorded analytics counter metric_type=%s target=%s date=%s",
            metric_type.value, target, today,
        )
    except Exception as exc:
        # Deliberately broad: ANY failure here (Mongo down, transient
        # network blip, an unexpected type on `target`, etc.) must be
        # swallowed - this function is never allowed to raise into a
        # tool-serving/job-completion critical path (ADR-023).
        # `logger.exception` (not `.error`) so the stack trace actually
        # lands in the logs - matches `app/worker.py`'s own
        # unexpected-error branch.
        logger.exception(
            "Failed to record analytics counter (metric_type=%s, target=%s): %s",
            metric_type.value, target, exc,
        )


async def record_tool_usage(tool_slug: str, tier: str) -> None:
    """Increment today's `tool_usage` counter for `tool_slug`.

    `tool_slug` should match the frontend's `tools-registry.ts` slug (e.g.
    `"merge-pdf"`, `"url-encoder-decoder"`) so future dashboard/analytics
    consumers can join against it directly.

    `tier` (`"tier1"`/`"tier2"`) is accepted per the approved spec's function
    signature and logged for observability/debugging only - it is NOT
    persisted on the `analytics_counters` document. The schema
    (`AnalyticsCounterBase`, `extra="forbid"`) only models
    `(metric_type, target, date, count)`; a future round wanting a
    per-tier breakdown needs its own explicit design/field, not something
    bolted on here without reopening that question.

    Fire-and-forget by contract (never raises) - see this module's
    docstring for the calling conventions this supports.
    """
    if not tool_slug:
        logger.warning("record_tool_usage called with empty tool_slug (tier=%s) - skipping", tier)
        return
    logger.debug("record_tool_usage tool_slug=%s tier=%s", tool_slug, tier)
    await _increment_counter(MetricType.TOOL_USAGE, tool_slug)


async def record_page_view(path: str) -> None:
    """Increment today's `page_view` counter for `path`.

    `path` is a page path only (e.g. `"/blog/some-post"`), never a full URL
    with host/query string - see `app/routers/analytics.py`'s
    `PageViewRequest` for the input shape this is validated against before
    it ever reaches here. No IP/session/user-identifying field is accepted
    or stored anywhere in this call (ADR-023's privacy posture) - `path` is
    the only input.

    Fire-and-forget by contract (never raises) - see this module's
    docstring for the calling conventions this supports.
    """
    if not path:
        logger.warning("record_page_view called with empty path - skipping")
        return
    await _increment_counter(MetricType.PAGE_VIEW, path)


async def get_summary(metric_type: MetricType, days: int, target: Optional[str] = None) -> list[dict]:
    """Read `analytics_counters` for `metric_type` over the last `days` UTC
    days (inclusive of today), grouped by `target`, for the Admin Dashboard's
    graphs (`GET /v1/analytics/summary`, `app/routers/analytics.py`).

    Returns a list of `{"target": str, "data": [{"date": "YYYY-MM-DD",
    "count": int}, ...]}` - one entry per distinct `target` actually found
    in the query result, each `data` list sorted ascending by date. Returns
    `[]` for a metric_type/target combination with zero matching documents -
    this is the expected, normal case for most tools right now (see this
    endpoint's approved spec's "Data-sparsity scope"), not an error.

    Uses the existing `(metric_type, target, date)` compound index (see
    `app/schemas/analytics_counter.py`) - `metric_type` (and `target`, when
    given) are equality-matched, `date` is the `$gte`/`$lte` range field,
    exactly the index's declared field order, so this is index-only, no new
    index needed.

    `date` range is computed as plain `YYYY-MM-DD` strings (today's UTC date
    back `days - 1` days) and compared lexicographically against the stored
    string field - see the schema module docstring for why that's a correct,
    index-friendly range query with no datetime parsing needed.
    """
    now = datetime.now(timezone.utc)
    end_date = now.strftime("%Y-%m-%d")
    start_date = (now - timedelta(days=days - 1)).strftime("%Y-%m-%d")

    query: dict = {
        "metric_type": metric_type.value,
        "date": {"$gte": start_date, "$lte": end_date},
    }
    if target:
        query["target"] = target

    cursor = db.analytics_counters.find(
        query, projection={"_id": 0, "target": 1, "date": 1, "count": 1}
    ).sort([("target", 1), ("date", 1)])

    # Plain dict, not defaultdict: insertion order (target-sorted, then
    # date-sorted within each target per the query's own sort) is preserved
    # and becomes the response's order for free.
    grouped: dict[str, list[dict]] = {}
    async for doc in cursor:
        grouped.setdefault(doc["target"], []).append({"date": doc["date"], "count": doc["count"]})

    return [{"target": t, "data": data} for t, data in grouped.items()]
