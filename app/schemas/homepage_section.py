"""Pydantic models for the `homepage_sections` collection.

New collection for the Homepage Sections CMS (`admin` module, ADR-019 —
founder-approved architecture decisions), flagged per CLAUDE.md's "don't
invent a new collection without flagging it" rule, same convention
`admin_users`/`ai_tools_usage`/`seo_tools_usage` were flagged under
(app/core/database.py::ensure_indexes has the running list).

Backs the small, admin-editable set of sections rendered on the public
homepage (hero banner, promo/announcement banners, the tool grid, ad
slots) — currently hardcoded in `frontend/app/page.tsx`; this collection
lets an admin reorder/toggle/edit them without a code deploy. A handful of
documents at most, not a growing/unbounded collection.

Collection: `homepage_sections` (lowercase-plural, matches this repo's
other collections). Fields: snake_case, matching this feature's own
`admin_users` collection (see that module's docstring) rather than the
camelCase convention Handbook Part C.9 describes for `files`/`jobs`/etc —
snake_case was specified directly in the approved architecture decisions
for this feature (ADR-019 work), so it's followed here too for
consistency within the same feature rather than mixing conventions across
`admin`-module collections.

**Indexing decisions** (see `app/core/database.py::ensure_indexes` for the
actual index creation):

- `order` (non-unique, ascending): both the public read path
  (`find({enabled: True}).sort("order")`) and the admin list path
  (`find({}).sort("order")`) sort on this field, so it's indexed to back
  both. NOT compounded with `enabled` — this collection is expected to
  stay tiny (a handful of sections, not thousands per Handbook C.9's
  "index only fields that are actually queried" guidance), so a full
  collection scan + in-memory sort/filter is already sub-millisecond;
  a compound `(enabled, order)` index would add write-side maintenance
  cost for no measurable read-side benefit at this scale. Revisit if the
  section catalog ever grows into the hundreds (e.g. per-locale variants).
- `order` is deliberately **NOT unique**. The reorder endpoint
  (backend-builder's route code) rewrites many documents' `order` values
  in one bulk operation — e.g. swapping two sections' positions means, for
  a brief moment mid-batch, two documents can hold the same `order` value
  before the batch finishes. A unique index would reject that transient
  state (an ordered `bulk_write` would abort partway through with a
  `DuplicateKeyError`; an unordered one would leave a partially-applied,
  inconsistent reorder). `order` is a display-position hint, not an
  identity field, so a few milliseconds of duplicate/skipped values mid
  reorder is harmless — worst case the public page reads a slightly stale
  or transiently-duplicated order until the batch completes. Left to
  application-layer bulk-write logic (backend-builder) to keep values
  eventually consistent (e.g. renumber 0..N-1 on every reorder call)
  rather than enforced at the DB layer.
- `type` gets a **partial unique index** scoped to `{"type": "tool_grid"}`
  only (`partialFilterExpression`). Exactly one `tool_grid` document must
  always exist — it's structural (the frontend's tool grid section is
  registry-driven from `frontend/lib/tools-registry.ts`, never
  admin-authored; deleting or duplicating it would break the homepage).
  This is cheap, compliance-sensitive insurance at the DB layer: even if
  the route layer's DELETE-rejection logic (backend-builder, per spec) has
  a bug, MongoDB itself refuses a second `tool_grid` insert. It does NOT
  block reorder/enable-toggle/content-edit writes (those never touch
  `type`), so it can't collide with the `order` bulk-write above. Seeding
  (`backend/scripts/seed_homepage_sections.py`, not part of this task)
  must insert its one `tool_grid` document exactly once — a second seed
  run inserting a second `tool_grid` document will fail this index, which
  is the intended behavior (fail loudly, don't silently duplicate).
- No TTL index: unlike `files`/`jobs`/`ai_tools_usage`, homepage sections
  have no natural expiry (Handbook C.9's TTL rule doesn't apply — this is
  structural site config, not transient processing/upload metadata).

**Content shape enforcement:** `content`'s shape varies by `type` (hero /
banner / ad_slot / tool_grid). `HomepageSectionBase` validates `content`
against a per-`type` sub-model (`HeroContent`/`BannerContent`/
`AdSlotContent`/`ToolGridContent`) via a `model_validator`, so a
mismatched or malformed `content` payload is rejected by Pydantic before
it ever reaches Mongo — this is the schema-level enforcement of the
`tool_grid` invariant called out in this feature's spec: `ToolGridContent`
is an empty, `extra="forbid"` model, so `content` for a `type: "tool_grid"`
document can only ever validate as `{}`. This is defense-in-depth on top
of (not a replacement for) the application-layer rule that nothing ever
writes to a `tool_grid` document's `content` in the first place (it's
registry-driven from the frontend, never admin-authored).

`type` is intentionally absent from `HomepageSectionUpdate` — a section's
`type` is fixed at creation (by the seed script) and never changes via the
admin CRUD/reorder routes; only `order`, `enabled`, and `content` are
admin-editable in place.
"""

from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.common import PyObjectId


class SectionType(str, Enum):
    HERO = "hero"
    BANNER = "banner"
    TOOL_GRID = "tool_grid"
    AD_SLOT = "ad_slot"


class BannerLink(BaseModel):
    """Optional call-to-action link inside a banner section."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(..., min_length=1, max_length=100)
    href: str = Field(..., min_length=1, max_length=2048)


class HeroContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    heading: str = Field(..., min_length=1, max_length=200)
    subheading: Optional[str] = Field(default=None, max_length=500)


class BannerContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    message: str = Field(..., min_length=1, max_length=500)
    style: str = Field(default="info", pattern="^(info|announcement)$")
    link: Optional[BannerLink] = None


class AdSlotContent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    placement_id: str = Field(..., min_length=1, max_length=100)
    height_px: int = Field(..., gt=0, le=2000)


class ToolGridContent(BaseModel):
    """Deliberately empty and closed (`extra="forbid"`). `tool_grid`
    content is entirely registry-driven from
    `frontend/lib/tools-registry.ts`, never admin-authored (see this
    module's docstring) — this is the schema-level enforcement of that
    invariant: any attempt to write a non-empty `content` object for a
    `tool_grid` section fails Pydantic validation before it ever reaches
    Mongo."""

    model_config = ConfigDict(extra="forbid")


_CONTENT_MODEL_BY_TYPE: dict[SectionType, type[BaseModel]] = {
    SectionType.HERO: HeroContent,
    SectionType.BANNER: BannerContent,
    SectionType.AD_SLOT: AdSlotContent,
    SectionType.TOOL_GRID: ToolGridContent,
}


class HomepageSectionBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: SectionType = Field(..., description="Immutable after creation — fixed by the seed script")
    order: int = Field(..., ge=0, description="Display position; admin-editable via the reorder endpoint")
    enabled: bool = Field(default=True, description="Visibility toggle for the public homepage")
    content: dict[str, Any] = Field(
        default_factory=dict,
        description="Shape varies by `type` — validated against a per-type sub-model below",
    )

    @model_validator(mode="after")
    def _validate_content_shape(self) -> "HomepageSectionBase":
        model_cls = _CONTENT_MODEL_BY_TYPE[self.type]
        try:
            validated = model_cls(**self.content)
        except Exception as exc:
            raise ValueError(
                f"content does not match the '{self.type.value}' section shape: {exc}"
            ) from exc
        # Round-trip through the typed sub-model so defaults are applied and
        # storage always holds a normalized shape (e.g. always {} for
        # tool_grid, never a stray empty-but-extra-keyed dict — extra keys
        # are already rejected above by extra="forbid").
        self.content = validated.model_dump(exclude_none=True)
        return self


class HomepageSectionCreate(HomepageSectionBase):
    """Shape used when inserting a new `homepage_sections` document (seed
    script, and the admin "create section" route if one is added)."""

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class HomepageSectionUpdate(BaseModel):
    """Partial-update shape for admin edits (toggle `enabled`, edit
    `content`, or a single-document `order` change outside the bulk
    reorder endpoint). All fields optional; `type` is deliberately absent
    (immutable — see module docstring). `updated_at` is stamped by the
    service layer on every write, never accepted from the caller.

    Note: `content` validation here is intentionally shallow (just a dict)
    — the service layer is expected to merge this onto the existing
    document's `type` and re-validate through `HomepageSectionBase` before
    writing, so the per-type shape check above still applies to updates,
    not just creates.
    """

    model_config = ConfigDict(extra="forbid")

    order: Optional[int] = Field(default=None, ge=0)
    enabled: Optional[bool] = None
    content: Optional[dict[str, Any]] = None


class HomepageSectionDocument(HomepageSectionBase):
    """Shape of a `homepage_sections` document as read back from MongoDB."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    created_at: datetime
    updated_at: datetime


class HomepageSectionReorderItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(..., description="homepage_sections._id as a string")
    order: int = Field(..., ge=0)


class HomepageSectionReorderRequest(BaseModel):
    """Body shape for the bulk reorder endpoint — see this module's
    docstring for why `order` is not DB-uniquely-constrained."""

    model_config = ConfigDict(extra="forbid")

    sections: list[HomepageSectionReorderItem] = Field(..., min_length=1)
