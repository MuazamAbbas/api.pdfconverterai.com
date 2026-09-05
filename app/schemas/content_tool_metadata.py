"""Pydantic models for the `content_tool_metadata` collection.

New collection for the Tools Metadata CMS (`content` module, ADR-021's
already-built foundation — feature spec approved 2026-09-04, see
`docs/roadmap/SPRINT_STATUS.md`'s 2026-09-04 entry), flagged per CLAUDE.md's
"don't invent a new collection without flagging it" rule, same convention
`content_categories`/`tags`/`admin_users`/`homepage_sections` were flagged
under (`app/core/database.py::ensure_indexes` has the running list).

Backs one editable marketing/SEO-metadata row per existing tool (title,
description, category, icon, tags, how-to-use/FAQ content, ad slot) so an
admin can change what a tool page's `<title>`/meta description/on-page copy
say without a code deploy. `frontend/lib/tools-registry.ts` remains the
authoritative code-level mapping of URL -> real interactive component; this
collection only supplies the marketing content layer read at render time to
override/supplement it (spec's own framing) - it never drives routing or
which React component mounts.

Collection: `content_tool_metadata` (lowercase-plural, matches this repo's
other collections). Fields: snake_case, matching `content_categories`'/
`tags`'/`homepage_sections`' convention within this same `content`-module
feature family (see `content_category.py`'s docstring for why this diverges
from Handbook Part C.9's camelCase default for `files`/`jobs`/etc).

**`slug`** is the primary lookup key (`GET /v1/content/tool-metadata/{slug}`)
and is **immutable after creation** (spec AC3), same reasoning
`ContentCategoryUpdate` omits `content_type`/`color_token` for and
`HomepageSectionUpdate` omits `type` for - `ContentToolMetadataUpdate` below
deliberately has no `slug` field, and the service layer (backend-builder's
next step) must never accept one in a merge-update payload.

Slug-derivation assumption (documented here for backend-builder, not
enforced by this schema layer - the service/route layer owns actually
deriving it): defaults from the last path segment of the tool's
`tools-registry.ts` `href`, e.g. `/tools/pdf/pdf-merger` -> `pdf-merger`,
`/tools/word-counter` -> `word-counter` (some registry hrefs have no
category segment - see that file). This mirrors how the href is already the
de facto unique identifier for a tool page today; `slug` is expected to
equal it verbatim, not re-slugified, since every existing href segment is
already a valid lowercase-hyphenated slug.

**`category`** stores a `content_categories.slug` string value - **this
schema layer intentionally does not query the database to validate it**.
The service layer (mirroring `categories_service.py`'s existing
`list_categories(ContentType.TOOL_METADATA)`) is responsible for resolving
this value against the live `content_categories` collection and rejecting
the write outright if it does not match one of the 10 seeded
`content_type: "tool_metadata"` rows (spec AC1) - never silently accepted,
never silently coerced to a default category. A plain `str` field here is
deliberate: baking the 10 category slugs into a `Literal`/`Enum` at the
schema layer would require a code change (and therefore a deploy) every
time `tools-registry.ts`'s category list changes, defeating the entire
point of this being admin-editable CMS content rather than code - the same
"code-owned, not duplicated into a rigid schema" reasoning ADR-021 already
applied to why `content_categories` itself isn't hardcoded as an enum.

**`icon`** is constrained to a fixed whitelist (`ALLOWED_TOOL_ICONS` below)
of lucide-react icon names, mirrored from `frontend/lib/tools-registry.ts`'s
actual `lucide-react` import list as of this writing. Unlike `category`
above, this is enforced at the schema layer (not deferred to a service-side
DB lookup) because the icon whitelist has no natural "grows independently of
a code deploy" story the way categories/tags do - a genuinely new icon
choice always requires a corresponding `tools-registry.ts` frontend import
change anyway (the icon component itself has to be imported and rendered
somewhere), so there's no meaningful risk of this whitelist blocking a
legitimate admin edit that the frontend couldn't already render. This
whitelist **must be kept in sync by hand** whenever `tools-registry.ts`'s
`lucide-react` import list changes - the same manual-sync trade-off ADR-021
already accepted for `content_categories`' seed data tracking
`tools-registry.ts`'s category list (its docstring's "Manual sync burden,
not automatic" trade-off applies here too). An icon name outside this set
fails loud (a Pydantic `ValueError`), never silently falls back to a
default icon - matching this feature family's "fail loud, no silent no-op"
convention (`CategoryReadOnly`, `ToolGridContent`'s `extra="forbid"`, etc.).

**`tags`** stores canonical tag *slugs* only - **never raw admin-typed
strings**. Same contract `tags.py`'s docstring documents for the entire
`content` module: the service layer must run every incoming raw tag string
through `app.services.content.tags_service.get_or_create_tag` first and
store only the returned `TagDocument.slug` values here. This schema layer
does not itself normalize or validate against `db.tags` - it only shapes
the list as `list[str]`, matching how `category` above is validated by
schema-shape only, DB-resolution by the service layer.

**`how_to_use`** / **`faq`**: both plain text/markdown strings, no rich-text
editor and no structured block format (spec's explicit "no rich text
editor" - the separate, still-queued Blog/News CMS may need a richer editor
later; not anticipated here). `faq` is stored as a single markdown string
rather than a structured `list[{question, answer}]` array: the spec text
says "plain text/markdown" for both fields side by side without singling
`faq` out for different treatment, and a single markdown string keeps this
foundation's storage shape uniform between the two fields (same admin
textarea-style editing experience, same fallback-to-empty behavior on read)
rather than introducing a second, structurally different content shape for
what the spec otherwise treats as a matched pair. If a future task finds
admins actually want per-question editable rows (e.g. for FAQ schema.org
markup), that's a schema migration for that task to make deliberately, not
guessed at here.

**`ad_slot`** reuses `AdSlotContent` (`placement_id`, `height_px`) from
`app/schemas/homepage_section.py` directly, per spec ("ad_slot reusing
AdSlotContent") - not redefined here, so the two features can never drift
on this shape.

**Indexing decisions** (see `app/core/database.py::ensure_indexes` for the
actual index creation):

- `slug` (unique, ascending): the primary lookup key for
  `GET /v1/content/tool-metadata/{slug}`, and doubles as the DB-layer
  backstop against a duplicate-slug create ever succeeding (the same
  insurance role `content_categories_slug_unique`/`tags_slug_unique` play
  for their own collections) - a service-layer find-before-insert check, if
  one is added, would still only be a TOCTOU-vulnerable first line of
  defense; this index is the actual guarantee.
- No index on `category`. At most ~57 documents will ever exist in this
  collection (one per tool in `tools-registry.ts`, per the spec's AC4 "all
  57 tool pages" scope), and there is no planned query that filters this
  collection *by* category at read time - the public read path is always a
  single `find_one({"slug": ...})` by the unique key above, and any future
  admin "list tools by category" view would be a full collection scan over
  at most ~57 documents, already sub-millisecond. Same "index only fields
  that are actually queried, tiny collection" reasoning documented for
  `content_categories_order`/`homepage_sections_order` - revisit only if a
  category-filtered list query is actually added and the collection has
  grown enough for a scan to matter (neither is expected: tool count grows
  with the product roadmap, not organically at scale).
- No TTL index: this is structural per-tool marketing content, not
  transient processing/upload metadata (Handbook C.9's TTL rule doesn't
  apply) - a tool's CMS row should persist indefinitely until an admin (or
  a future tool-removal task) explicitly deletes it.
"""

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import PyObjectId
from app.schemas.homepage_section import AdSlotContent

# Mirrors `frontend/lib/tools-registry.ts`'s actual `lucide-react` import
# list as of this writing (2026-09-05) - see this module's docstring for why
# this must be kept in sync by hand rather than queried/generated. Keep
# alphabetized to make future diffs against tools-registry.ts's import list
# easy to eyeball.
ALLOWED_TOOL_ICONS: frozenset[str] = frozenset(
    {
        "Bot",
        "Calculator",
        "Download",
        "FileText",
        "Globe",
        "Image",
        "KeyRound",
        "Merge",
        "QrCode",
        "Scale",
        "Search",
        "Shield",
        "Split",
        "Sparkles",
        "Type",
        "Video",
    }
)


class ContentToolMetadataBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "Immutable after creation (spec AC3). Defaults from the last path "
            "segment of the tool's tools-registry.ts href - see module "
            "docstring for the exact derivation assumption. Not re-derived or "
            "re-validated against the registry by this schema layer."
        ),
    )
    title: str = Field(..., min_length=1, max_length=100)
    category: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "A content_categories.slug value. NOT validated against the "
            "database at this schema layer - the service layer must resolve "
            "this against content_categories filtered to "
            "content_type='tool_metadata' (one of the 10 seeded slugs only) "
            "and reject the write otherwise. See module docstring."
        ),
    )
    icon: str = Field(
        ...,
        description=(
            "A lucide-react icon name, restricted to ALLOWED_TOOL_ICONS "
            "(validated below). See module docstring for the manual-sync "
            "requirement against tools-registry.ts's lucide-react imports."
        ),
    )
    description: str = Field(..., min_length=1, max_length=500)
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Canonical tag slugs only - never raw strings. The service layer "
            "must run every incoming raw tag through "
            "app.services.content.tags_service.get_or_create_tag before "
            "storing its slug here. See module docstring."
        ),
    )
    how_to_use: Optional[str] = Field(
        default=None,
        max_length=10000,
        description="Plain text/markdown - no rich text editor (spec AC1).",
    )
    faq: Optional[str] = Field(
        default=None,
        max_length=10000,
        description=(
            "Plain text/markdown, single field (not a structured Q&A array) "
            "- see module docstring for why."
        ),
    )
    ad_slot: Optional[AdSlotContent] = Field(
        default=None,
        description="Reuses AdSlotContent (placement_id, height_px) from homepage_section.py verbatim.",
    )

    @field_validator("icon")
    @classmethod
    def _validate_icon_whitelist(cls, value: str) -> str:
        if value not in ALLOWED_TOOL_ICONS:
            raise ValueError(
                f"icon {value!r} is not in ALLOWED_TOOL_ICONS - it must be a lucide-react icon "
                "name already imported in frontend/lib/tools-registry.ts (see this module's "
                "docstring for the manual-sync requirement)"
            )
        return value


class ContentToolMetadataCreate(ContentToolMetadataBase):
    """Shape used when inserting a new `content_tool_metadata` document via
    the `require_admin`-gated `POST /v1/content/tool-metadata` route
    (backend-builder's next step, not part of this task)."""

    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ContentToolMetadataUpdate(BaseModel):
    """Partial-update shape for admin edits. `slug` is deliberately absent -
    immutable after creation (spec AC3), same reasoning
    `ContentCategoryUpdate` omits `content_type`/`color_token` for and
    `HomepageSectionUpdate` omits `type` for. `updated_at` is stamped by the
    service layer on every write, never accepted from the caller.

    `category`/`icon`/`tags` still go through the same validation as create
    (category DB-resolution and tag normalize-and-upsert stay service-layer
    concerns; icon whitelist is enforced here at the schema layer) - the
    service layer is expected to merge this onto the existing document and
    re-validate through `ContentToolMetadataBase`'s rules before writing,
    same convention `HomepageSectionUpdate`'s docstring documents for its
    own shallow `content` field.
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=100)
    category: Optional[str] = Field(default=None, min_length=1, max_length=100)
    icon: Optional[str] = None
    description: Optional[str] = Field(default=None, min_length=1, max_length=500)
    tags: Optional[list[str]] = None
    how_to_use: Optional[str] = Field(default=None, max_length=10000)
    faq: Optional[str] = Field(default=None, max_length=10000)
    ad_slot: Optional[AdSlotContent] = None

    @field_validator("icon")
    @classmethod
    def _validate_icon_whitelist(cls, value: Optional[str]) -> Optional[str]:
        if value is not None and value not in ALLOWED_TOOL_ICONS:
            raise ValueError(
                f"icon {value!r} is not in ALLOWED_TOOL_ICONS - it must be a lucide-react icon "
                "name already imported in frontend/lib/tools-registry.ts (see module docstring "
                "for the manual-sync requirement)"
            )
        return value


class ContentToolMetadataDocument(ContentToolMetadataBase):
    """Shape of a `content_tool_metadata` document as read back from
    MongoDB."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    created_at: datetime
    updated_at: datetime
