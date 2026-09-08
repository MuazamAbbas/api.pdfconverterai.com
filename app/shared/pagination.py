"""Generic page-number pagination utility.

This is the **first paginated list endpoint** in this codebase. Every list
function that existed before this module (`categories_service.list_categories`,
`tags_service.list_tags`, `tool_metadata_service.list_all`) returns its entire
result set unbounded, because each of those collections is a small, bounded
set by construction: `content_categories` stays at roughly 10-20 rows forever
(a fixed taxonomy), `tags` is unbounded in theory but only ever read via a
cheap full-scan autocomplete list, and `content_tool_metadata` is hard-capped
at the tool count (~57, one row per tool - it cannot grow faster than the
product's own tool catalogue). None of them needed a page/limit contract.

The first collection that actually breaks that "small, bounded set" assumption
is `content_blog_posts` (Blog/News CMS) - an ordinary editorial cadence adds
posts indefinitely over the site's lifetime, so returning the entire
collection on every list request stops being viable at some point. This
module exists to serve that need.

It is deliberately written as a **generic, reusable utility** rather than
something shaped only around blog posts - the founder has flagged that the
tools registry (`frontend/lib/tools-registry.ts`, currently ~56-57 tools) is
a plausible second consumer once it grows past the point where an unbounded
list response is still cheap/small enough to not matter. That future tools-
listing endpoint is NOT being built by this change - this module just avoids
painting the codebase into a blog-post-only corner by keeping every name,
docstring, and type signature here free of any blog-specific vocabulary. Any
future list endpoint (blog posts, tools, or otherwise) that needs page/limit
semantics should depend on `PaginationParams` and call `paginated_envelope`
directly rather than reimplementing this shape.

This module only builds the *request-parsing* and *response-shaping* halves
of pagination - it deliberately does not touch MongoDB at all (no `.skip()`/
`.limit()` here). Each service module remains the sole owner of its own
collection's queries (Handbook Part C.3's "one module owns its collection"
rule) and is expected to call `.skip(params.offset).limit(params.page_size)`
(or equivalent) itself using the plain ints this class exposes.
"""
from typing import Any

from fastapi import Query
from pydantic import BaseModel

# Defaults chosen to be sane for a content-list UI (a blog index page, or any
# future paginated list) rather than tuned to any one caller:
#   - `page` defaults to 1 (the first page) and is 1-indexed, matching the
#     conventional "page 1" UX vocabulary rather than a 0-indexed offset,
#     which would be a confusing default for anything constructing URLs like
#     `?page=1` by hand.
#   - `page_size` defaults to 10 - enough to fill a typical index page/grid
#     without being so large that an unauthenticated public endpoint could be
#     used to cheaply exfiltrate an entire growing collection in one request.
#   - `page_size` is capped at 50 (`le=50` below) for the same reason: even a
#     legitimate caller asking for a bigger page than the UI needs shouldn't
#     be able to turn this into an unbounded-list endpoint by setting
#     `page_size=999999999`, which would defeat the entire point of adding
#     pagination in the first place.
DEFAULT_PAGE = 1
DEFAULT_PAGE_SIZE = 10
MAX_PAGE_SIZE = 50


class PaginationParams:
    """FastAPI dependency that parses and validates `page`/`page_size` query
    params shared by any paginated list route.

    Usage: `async def list_things(pagination: PaginationParams = Depends()):`
    (FastAPI resolves each `__init__` parameter as its own `Query(...)`
    dependency, the standard pattern for a class-based dependency with query
    parameters - no separate `Depends(...)` wrapper function needed).

    Exposes `.offset` as a convenience for the `.skip()` value a Mongo query
    needs, computed once here rather than re-derived by every caller.
    """

    def __init__(
        self,
        page: int = Query(default=DEFAULT_PAGE, ge=1, description="1-indexed page number"),
        page_size: int = Query(
            default=DEFAULT_PAGE_SIZE,
            ge=1,
            le=MAX_PAGE_SIZE,
            description=f"Items per page (max {MAX_PAGE_SIZE})",
        ),
    ) -> None:
        self.page = page
        self.page_size = page_size

    @property
    def offset(self) -> int:
        """Number of items to skip to reach this page - the `.skip()` value
        for a Mongo query, given a fixed `page_size` and 1-indexed `page`."""
        return (self.page - 1) * self.page_size


class PaginatedData(BaseModel):
    """Generic paginated list shape, nested as the `data` value of the
    standard `envelope()` response (`app.shared.responses.envelope`) - see
    `paginated_envelope` below, which builds one of these and wraps it.

    Deliberately untyped (`list[Any]`) rather than `Generic[T]` - callers
    already shape each item into a plain dict (mirroring every other
    `_..._out()` helper in this codebase, e.g. `content.py`'s
    `_tool_metadata_out`) before handing it to `paginated_envelope`, so there
    is no pydantic model per item to parametrize over here.
    """

    items: list[Any]
    total: int
    page: int
    page_size: int
    total_pages: int


def paginated_envelope(items: list, total: int, params: PaginationParams) -> dict:
    """Builds the `data` payload for a paginated list response:
    `{"items": [...], "total": N, "page": N, "page_size": N, "total_pages": N}`.

    `total_pages` is derived from `total`/`page_size` here (ceiling division)
    rather than trusted from any caller input, so it always agrees with the
    other three numbers.

    Callers are expected to pass this dict straight through as
    `envelope()`'s `data` argument, e.g.:
        `return envelope(True, "...", data=paginated_envelope(items, total, pagination))`
    """
    total_pages = (total + params.page_size - 1) // params.page_size if total > 0 else 0
    return PaginatedData(
        items=items,
        total=total,
        page=params.page,
        page_size=params.page_size,
        total_pages=total_pages,
    ).model_dump()
