"""`analytics` module HTTP surface (ADR-023: Analytics Module Foundation -
Daily Counter Aggregation, Async In-Process Writes).

Service logic (the `db.analytics_counters` upsert, the "never raises"
contract) lives in `app/analytics/service.py`, not here - this file only
does HTTP concerns (request validation, wiring the fire-and-forget
`BackgroundTask`), same division of responsibility every other router in
this codebase follows for its matching `app/services/<module>/` package.
`analytics` keeps its service layer at `app/analytics/service.py` instead of
`app/services/analytics/service.py` because that's the exact path
`app/schemas/analytics_counter.py`'s already-approved module docstring
commits to - this router file stays under `app/routers/`, matching every
sibling router's location and how `app/main.py` imports/registers routers,
so this is a deliberate one-file exception, not a new whole-module layout
convention to copy.

Round 1 had a single route: `POST /v1/analytics/pageview` - the frontend's
page-view beacon (see the approved spec's "Ingestion path"). Registered in
`app/main.py` WITH `protected_dependency` (`verify_api_key` + per-key rate
limiting), exactly like every other tool router - this is not a
public/unauthenticated route the way `content.public_router`/
`admin.public_router` are, since only the frontend's own server-side proxy
(the `frontend-service` API key) is meant to call it, not arbitrary
end-user clients. New router -> the `frontend-service` API key needs its
`categories` array updated to include `"analytics"` via `$addToSet` before
this is usable in production (Handbook Part D.1 / CLAUDE.md's standing
grant-gap checklist - see this task's PR description for the exact
mechanism and current status).

**Privacy (ADR-023, non-negotiable):** the only input accepted by
`POST /pageview` is `path`. No IP, session id, user id, user-agent, or any
other identifying field is read, logged, or stored by this route or by
`record_page_view` downstream.

Round 2 (2026-09-16 "Spec approved: Admin Dashboard analytics visualization
(graphs)") adds `GET /v1/analytics/summary` - a read-only query for the
Admin Dashboard's graphs. Unlike `/pageview`, this is admin-dashboard data,
not a tool action or a frontend-service beacon, so it carries its own
`Depends(require_admin)` on top of the router-level `protected_dependency`
from `app/main.py` - the exact defense-in-depth convention
`app/routers/admin.py`'s module docstring documents (two independent auth
layers on every admin-only route, not one). The `frontend-service` API
key's existing `"analytics"` category grant already covers the router-level
`x-api-key` layer for this new route too (grants are router-wide per
CLAUDE.md/Handbook Part D.1, not per-endpoint) - no additional grant work
needed for this addition, only the `require_admin` layer is new.
"""
import logging
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel, Field

from app.analytics.service import get_summary, record_page_view
from app.core.admin_auth import require_admin
from app.schemas.analytics_counter import MetricType
from app.shared.responses import envelope

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/analytics", tags=["Analytics"])


class PageViewRequest(BaseModel):
    # Deliberately a path only - never a full URL (no host/query string to
    # accidentally carry a tracking parameter through to storage). Matches
    # `AnalyticsCounterBase.target`'s `max_length=300` bound.
    path: str = Field(
        ...,
        min_length=1,
        max_length=300,
        pattern=r"^/",
        description="A page path only, e.g. '/blog/some-post' - not a full URL.",
    )


@router.post(
    "/pageview",
    summary="Record one page view (fire-and-forget, aggregate counts only)",
)
async def record_pageview(payload: PageViewRequest, background_tasks: BackgroundTasks):
    # Fire-and-forget: queued to run after this response is sent, and
    # `record_page_view` never raises internally (see
    # app/analytics/service.py) - this adds no response latency and no
    # failure risk regardless of Mongo's state.
    background_tasks.add_task(record_page_view, payload.path)
    logger.debug("Queued page_view analytics increment for path=%s", payload.path)
    return envelope(True, "Page view recorded", data=None)


@router.get(
    "/summary",
    summary="Admin: per-day counts by target for a metric_type over a date range",
)
async def get_analytics_summary(
    metric_type: MetricType,
    days: int = Query(30, ge=1, le=90, description="Number of trailing UTC days to include, inclusive of today."),
    target: Optional[str] = Query(
        None,
        min_length=1,
        max_length=300,
        description="Filter to a single tool slug (tool_usage) or page path (page_view). Omit for all targets.",
    ),
    admin: dict = Depends(require_admin),
):
    # Admin-dashboard-only read: `require_admin` here, on top of the
    # router-level `protected_dependency` from app/main.py - same
    # defense-in-depth convention app/routers/admin.py's writes use, not a
    # new pattern (see this file's module docstring).
    results = await get_summary(metric_type, days, target)
    logger.debug(
        "Admin %s read analytics summary metric_type=%s days=%d target=%s (%d targets returned)",
        admin.get("email"), metric_type.value, days, target, len(results),
    )
    return envelope(True, "Analytics summary retrieved", data=results)
