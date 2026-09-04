"""Pydantic models for the `content_categories` collection.

New collection for the shared content-taxonomy foundation (the new `content`
module, ADR-021 — founder-approved architecture decisions), flagged per
CLAUDE.md's "don't invent a new collection without flagging it" rule, same
convention `admin_users`/`homepage_sections`/`ai_tools_usage` were flagged
under (app/core/database.py::ensure_indexes has the running list).

Backs the two queued CMS systems' shared category taxonomy (Tools Metadata
CMS, Blog/News CMS — neither is built by this task; see ADR-021's Round-1
scope note). Two distinct kinds of row live in one collection, discriminated
by `content_type`:

- `content_type: "tool_metadata"` — one row per existing tool category
  (`frontend/lib/tools-registry.ts`'s 10 hardcoded categories). Seeded once
  by `backend/scripts/seed_content_categories.py`. **Read-only through the
  API** — the service layer (`app/services/content/categories_service.py`)
  rejects any edit/delete attempt on these rows with a dedicated
  `CategoryReadOnly` exception (translated to `CATEGORY_READ_ONLY` by the
  router backend-builder adds next), never a silent no-op. This mirrors
  `app/services/admin/homepage_sections_service.py`'s `delete_section`
  rejecting `type == "tool_grid"`.
- `content_type: "blog"` — genuinely mutable rows: full admin CRUD
  (create/rename/reorder/delete), free to grow independently of the tool
  taxonomy. Has no color-token identity (ADR-021's trade-offs section) —
  `color_token` must be `None` for every `blog` row, enforced below.

Collection: `content_categories` (lowercase-plural, matches this repo's
other collections). Fields: snake_case, matching ADR-021's own field list
and this codebase's other `admin`/`content`-adjacent collections
(`admin_users`, `homepage_sections`) rather than the camelCase convention
Handbook Part C.9 describes for `files`/`jobs`/etc — snake_case was
specified directly in ADR-021's Decision section, so it's followed here too
for consistency within the same feature family rather than mixing
conventions across these collections.

**Indexing decisions** (see `app/core/database.py::ensure_indexes` for the
actual index creation):

- `slug` (unique, ascending): a single global uniqueness constraint across
  *both* `content_type`s — not scoped/partial. This is deliberate: it also
  functions as the DB-layer backstop against a duplicate `tool_metadata`
  row ever being inserted twice (e.g. a buggy re-run of the seed script, or
  a future create-route bug), the same insurance role
  `homepage_sections_type_tool_grid_unique`'s partial index plays for the
  structural `tool_grid` section — except here it doesn't need to be
  partial, because slugs must be globally unique anyway (a `blog` category
  slug colliding with a `tool_metadata` one would be ambiguous for any
  future content item that references a category by slug alone).
- `order` (non-unique, ascending): both content types are listed sorted by
  `order` (`find({content_type: ...}).sort("order")`). NOT compounded with
  `content_type`, following `homepage_sections_order`'s exact precedent and
  reasoning: this collection is expected to stay small (10 fixed tool rows
  + a modest, admin-curated set of blog rows — not thousands per Handbook
  C.9's "index only fields that are actually queried" guidance), so a full
  collection scan + in-memory filter/sort is already sub-millisecond; a
  compound `(content_type, order)` index would add write-side maintenance
  cost for no measurable read-side benefit at this scale. Revisit if the
  blog category list ever grows into the hundreds.
- No TTL index: like `homepage_sections`, this is structural taxonomy data
  with no natural expiry (Handbook C.9's TTL rule doesn't apply).

**No DB-layer backstop for the edit/delete read-only constraint itself.**
Unlike the create-side duplicate-insert protection the unique `slug` index
gives for free, there is no vanilla MongoDB Community Edition mechanism
that can conditionally block an `update`/`delete` against only the
documents matching `{content_type: "tool_metadata"}` — collection
validators (`$jsonSchema`) validate document *shape*, not "this specific
existing document may never be written to again based on its own stored
field value", and role-based access control in Community Edition is
collection-level, not document-level (per-document ACLs are an
Atlas/Enterprise-only feature this VPS's self-hosted MongoDB doesn't have).
This is genuinely different from `homepage_sections_type_tool_grid_unique`,
which only ever backstopped *creation* of a second `tool_grid` document,
never blocked deleting the one that exists — the same limitation applies
here. The read-only guard is therefore application-layer only:
`categories_service.py`'s `update_category`/`delete_category` always load
the existing document first and check its `content_type` before writing,
raising `CategoryReadOnly` (fail loud, never a silent no-op) rather than
relying on any DB-level backstop for this specific invariant.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import PyObjectId


class ContentType(str, Enum):
    TOOL_METADATA = "tool_metadata"
    BLOG = "blog"


class ContentCategoryBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=100)
    slug: str = Field(..., min_length=1, max_length=100)
    content_type: ContentType
    color_token: Optional[str] = Field(
        default=None,
        max_length=100,
        description=(
            "Only meaningful for content_type='tool_metadata' rows, mirroring "
            "the `--category-{slug}` tokens in frontend/THEME.md. Always null "
            "for content_type='blog' rows (enforced below) - blog categories "
            "have no color-token identity per ADR-021."
        ),
    )
    order: int = Field(..., ge=0, description="Display position within its content_type")

    @model_validator(mode="after")
    def _validate_color_token_scope(self) -> "ContentCategoryBase":
        if self.content_type == ContentType.BLOG and self.color_token is not None:
            raise ValueError("color_token must be null for content_type='blog' rows (ADR-021)")
        return self


class ContentCategoryCreate(ContentCategoryBase):
    """Shape used when inserting a new `content_categories` document.

    Used both by `backend/scripts/seed_content_categories.py` (direct
    `db.content_categories` writes, bypassing the service layer entirely -
    same convention `seed_homepage_sections.py` follows for its structural
    `tool_grid` document) and by the future admin "create blog category"
    route via `categories_service.create_category`, which additionally
    rejects `content_type='tool_metadata'` at the service layer (see that
    module's docstring) since only the seed script may ever create those
    rows.
    """

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ContentCategoryUpdate(BaseModel):
    """Partial-update shape for admin edits to a `content_type='blog'` row
    (rename / reorder-in-place). `content_type` and `color_token` are
    deliberately absent - `content_type` is immutable after creation (same
    reasoning `HomepageSectionUpdate` gives for omitting `type`), and
    `color_token` is never settable through this path since the only rows
    this update shape is ever legitimately applied to (`blog`) must always
    have a null `color_token` (see `ContentCategoryBase`'s validator).

    `categories_service.update_category` rejects this update outright with
    `CategoryReadOnly` if the existing document's `content_type` is
    `tool_metadata`, regardless of which fields are set here.
    """

    model_config = ConfigDict(extra="forbid")

    label: Optional[str] = Field(default=None, min_length=1, max_length=100)
    slug: Optional[str] = Field(default=None, min_length=1, max_length=100)
    order: Optional[int] = Field(default=None, ge=0)


class ContentCategoryDocument(ContentCategoryBase):
    """Shape of a `content_categories` document as read back from MongoDB."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    created_at: datetime
    updated_at: datetime


class ContentCategoryReorderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="content_categories._id as a string")
    order: int = Field(..., ge=0)


class ContentCategoryReorderRequest(BaseModel):
    """Body shape for a future bulk reorder endpoint over `blog` rows only -
    see `homepage_section.py`'s `HomepageSectionReorderRequest` for why
    `order` is not DB-uniquely-constrained (identical reasoning applies
    here)."""

    model_config = ConfigDict(extra="forbid")

    categories: list[ContentCategoryReorderItem] = Field(..., min_length=1)
