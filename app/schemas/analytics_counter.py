"""Pydantic models for the `analytics_counters` collection.

New collection for the `analytics` module foundation (**ADR-023: Analytics
Module Foundation — Daily Counter Aggregation, Async In-Process Writes**;
founder-approved spec, `docs/roadmap/SPRINT_STATUS.md`'s 2026-09-15
"Spec approved: Analytics module (foundation)" entry), flagged per
CLAUDE.md's "don't invent a new collection without flagging it" rule, same
convention `ai_tools_usage`/`seo_tools_usage`/`admin_users`/
`homepage_sections`/`content_categories` were flagged under
(`app/core/database.py::ensure_indexes` has the running list) — except here
the collection itself is the explicit subject of the founder-approved spec
and ADR-023, not a from-scratch ad-hoc addition, so this docstring documents
where that decision lives rather than raising it fresh.

One document per unique `(metric_type, target, date)` combination — a
daily-bucketed pre-aggregation counter, not a raw per-event log (per
ADR-023's "Storage model" decision: bounded, traffic-independent growth,
matching the future dashboard's actual need for graphable per-day trends).
Written via an `$inc`/`$setOnInsert` upsert from `app/analytics/`
(backend-builder's next task, not built here), e.g.:

    await db.analytics_counters.update_one(
        {"metric_type": metric_type, "target": target, "date": today},
        {
            "$inc": {"count": 1},
            "$setOnInsert": {"created_at": now},
            "$set": {"updated_at": now},
        },
        upsert=True,
    )

Same counter-collection shape and upsert pattern as `ai_tools_usage`
(`app/services/ai/usage_limits.py`) and `seo_tools_usage`
(`app/services/seo/usage_limits.py`) — one document per key-tuple,
incremented via `$inc` — except keyed on `(metric_type, target, date)`
rather than `(apiKeyId, date)`/`(apiKeyId, hourBucket)`, and deliberately
**not** TTL-indexed (see below) — don't copy that part of the
`ai_tools_usage`/`seo_tools_usage` precedent here.

**Fields are snake_case, not Handbook Part C.9's camelCase default for
`files`/`jobs`/etc.** This mirrors the same deliberate divergence
`content_category.py`'s docstring documents for the `content` module's
collections: ADR-023 specifies snake_case directly (matching the
founder-approved spec's own field list — `metric_type`/`target`/`date`/
`count`/`created_at`/`updated_at` — and the exact upsert shape quoted in the
spec's task description), so it's followed here rather than mixing
conventions within the same collection.

- `metric_type`: `"tool_usage"` | `"page_view"` — Round 1 scope only, per
  the approved spec's explicit exclusion list (no referrer/UTM tracking,
  session/funnel analysis, per-user analytics, or error-rate analytics
  yet). Adding a third type later is an additive enum change, not a
  breaking one.
- `target`: a tool slug (e.g. `"merge-pdf"`, matching
  `frontend/lib/tools-registry.ts`) for `tool_usage`, or a page path (e.g.
  `"/blog/some-post"`) for `page_view`. Deliberately a plain string, not a
  discriminated/validated shape per `metric_type` — both metric types need
  nothing more than "non-empty string" validation for this field, unlike
  e.g. `content_pages`' per-block-type dispatch.
- `date`: `YYYY-MM-DD` string (UTC day bucket), **not a BSON `Date`** — it
  is used as an exact-match key alongside `metric_type`/`target`, not a
  range field queried against a `datetime`. The future dashboard's "last 30
  days" query still works against this string form: ISO `YYYY-MM-DD`
  strings sort and range-compare (`$gte`/`$lte`) lexicographically
  identically to their chronological order, so
  `{"date": {"$gte": "2026-08-16", "$lte": "2026-09-15"}}` is a correct and
  index-friendly range query with no need to parse/cast to a `datetime`
  first.
- `count`: integer, incremented via `$inc` — never set directly except the
  implicit `0 -> 1` of the first upsert.
- `created_at`/`updated_at`: standard timestamps per Handbook C.9's "every
  document gets createdAt/updatedAt" rule (snake_case per the above).
  Note for backend-builder's upsert: `created_at` belongs in
  `$setOnInsert` (set once, first write only); `updated_at` should be set
  on *every* write (inside `$set`, alongside the `$inc`), not
  `$setOnInsert`, so it actually tracks the most recent increment — the
  sample upsert quoted in the approved spec's task description only shows
  `$setOnInsert` for `created_at` and omits `updated_at` entirely; don't
  copy that omission verbatim into the real implementation.

**Indexing decisions** (see `app/core/database.py::ensure_indexes` for the
actual index creation):

- Unique compound index on `(metric_type, target, date)`, in that field
  order: this is both the upsert key (guarantees concurrent `$inc` upserts
  for the same tuple can never create duplicate counter documents — same
  insurance role `ai_tools_usage_apiKeyId_date`/`seo_tools_usage_apiKeyId_hourBucket`
  play for their own upserts) and the shape the future dashboard query
  needs (`find({"metric_type": ..., "target": ...})` filtered/sorted by
  `date` for a "last N days" trend). Field order matters: `metric_type` and
  `target` are always equality-matched by both the upsert and the
  dashboard query, so they lead; `date` is the range/sort field, so it's
  last — the standard MongoDB compound-index rule ("equality prefix, then
  range/sort field") this index needs to actually be used for the 30-day
  range query rather than falling back to a collection scan.
- **No TTL index — deliberate, confirmed against ADR-023 and the approved
  spec, not an oversight.** Unlike `files`/`jobs`/`ai_tools_usage`/
  `seo_tools_usage` (all transient: temp uploads, in-flight processing
  state, or a short support-window usage counter), this collection is a
  bounded, permanent aggregation table — its size is capped by
  (number of tools + page paths) x (days since launch), not by traffic
  volume, and the entire point of Round 1 is to let the future dashboard
  graph historical per-day trends indefinitely. A TTL index here would
  silently delete the exact historical data the dashboard exists to show.
  Revisit only if a deliberate retention/rollup policy is designed later
  (e.g. collapsing per-day counters into per-month ones after N days) —
  this is not a default to apply by copying the `files`/`jobs`/`*_usage`
  TTL pattern.
- No other index: no query shape beyond the one above is planned in Round 1
  (no per-`target`-only listing across all metric types, no per-`date`-only
  cross-tool rollup) — same "index only what's actually queried" posture
  Handbook C.9 and every sibling collection in this codebase already
  follow.

**No IP/session/user-identifying field anywhere on this schema** — per
ADR-023's privacy posture (aggregate counts only). Do not add one later
without explicitly reopening that question (see the approved spec's
"Privacy" section) — a future round wanting unique-visitor/session-based
metrics needs a new, separate design, not a field bolted onto this
collection.
"""

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PyObjectId


class MetricType(str, Enum):
    TOOL_USAGE = "tool_usage"
    PAGE_VIEW = "page_view"


class AnalyticsCounterBase(BaseModel):
    """Shared fields for the `(metric_type, target, date)` counter key.

    No `Create`/`Update` request shapes exist for this collection — unlike
    the `content` module's CMS collections, this is not an admin-authored
    resource with a REST create/edit surface. Every write is an internal
    `$inc` upsert from `app/analytics/`'s `record_tool_usage`/
    `record_page_view` (backend-builder's next task), never a direct
    document replace/PATCH from a caller.
    """

    model_config = ConfigDict(extra="forbid")

    metric_type: MetricType
    target: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description=(
            "A tool slug (e.g. 'merge-pdf') for metric_type='tool_usage', "
            "or a page path (e.g. '/blog/some-post') for "
            "metric_type='page_view'."
        ),
    )
    date: str = Field(
        ...,
        pattern=r"^\d{4}-\d{2}-\d{2}$",
        description=(
            "UTC day bucket, YYYY-MM-DD. Stored as a plain string (an "
            "exact-match/range-sortable key), not a BSON Date - see module "
            "docstring."
        ),
    )
    count: int = Field(
        ...,
        ge=0,
        description="Incremented via $inc. Never set directly except the implicit 0 -> 1 of the first upsert.",
    )


class AnalyticsCounterDocument(AnalyticsCounterBase):
    """Shape of an `analytics_counters` document as read back from MongoDB."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    created_at: datetime
    updated_at: datetime
