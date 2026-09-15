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

Single route for this round: `POST /v1/analytics/pageview` - the frontend's
page-view beacon (Round 1 scope, see the approved spec's "Ingestion path").
Registered in `app/main.py` WITH `protected_dependency` (`verify_api_key` +
per-key rate limiting), exactly like every other tool router - this is not
a public/unauthenticated route the way `content.public_router`/
`admin.public_router` are, since only the frontend's own server-side proxy
(the `frontend-service` API key) is meant to call it, not arbitrary
end-user clients. New router -> the `frontend-service` API key needs its
`categories` array updated to include `"analytics"` via `$addToSet` before
this is usable in production (Handbook Part D.1 / CLAUDE.md's standing
grant-gap checklist - see this task's PR description for the exact
mechanism and current status).

**Privacy (ADR-023, non-negotiable):** the only input accepted here is
`path`. No IP, session id, user id, user-agent, or any other
identifying field is read, logged, or stored by this route or by
`record_page_view` downstream.
"""
import logging

from fastapi import APIRouter, BackgroundTasks
from pydantic import BaseModel, Field

from app.analytics.service import record_page_view
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
