"""Pydantic models for the `content_pages` collection.

New collection for the Dynamic Pages builder (`content` module, ADR-021's
already-built foundation, extended by **ADR-022: Dynamic CMS Pages —
content-block model and route-collision handling**,
`docs/architecture/adr/ADR-022-dynamic-pages-content-model-and-routing.md`
— feature spec approved per `docs/roadmap/SPRINT_STATUS.md`'s 2026-09-12
entry), flagged per CLAUDE.md's "don't invent a new collection without
flagging it" rule, same convention `content_categories`/`tags`/
`content_tool_metadata`/`content_blog_posts` were flagged under
(`app/core/database.py::ensure_indexes` has the running list).

Unlike every prior CMS-backed collection in this codebase — `homepage_sections`
(ADR-019), `content_categories`/`content_tool_metadata`/`content_blog_posts`
(ADR-021) — which each render into a single, fixed, code-owned route/template,
this collection backs a genuinely new kind of thing: an admin can create a
brand-new page at an arbitrary slug, built from an ordered list of typed
content blocks. That is the whole reason ADR-022 exists (see its "Options
considered" section) rather than this being a bounded extension of ADR-021
the way the Blog/News CMS and per-tool-content specs both were. This file
generalizes `app/schemas/homepage_section.py`'s discriminated-union-by-type
pattern (`SectionType` / `_CONTENT_MODEL_BY_TYPE` / per-type sub-models /
`model_validator(mode="after")`) from "exactly one document per type" to
"an ordered list of many typed blocks per document."

Collection: `content_pages` (lowercase-plural, matches this repo's other
collections). Fields: snake_case, matching `content_categories`'/`tags`'/
`content_tool_metadata`'s/`content_blog_posts`'s convention within this same
`content`-module feature family (see `content_category.py`'s docstring for
why this diverges from Handbook Part C.9's camelCase default for
`files`/`jobs`/etc).

**Block-type reuse** (per ADR-022's decision to reuse already-reviewed shapes
verbatim wherever they already fit, rather than inventing a fourth way to
express the same content):

- `hero` reuses `HeroContent` from `app/schemas/homepage_section.py`,
  **imported, not redefined** — exact precedent: `content_blog_post.py`
  imports `AdSlotContent` from the same file rather than redefining it.
- `ad_slot` reuses `AdSlotContent` from the same file, same reasoning —
  already shared by `content_blog_post.py`'s `ad_slot` field, so three
  features now share this shape and can never drift on it independently.
- `cta_banner` reuses `BannerContent`/`BannerLink` from the same file
  (the homepage's `banner` section shape — message/style/link) — a page's
  call-to-action banner block is the same shape as the homepage's, so it's
  imported rather than redefined here too.
- `rich_text` is new (`RichTextContent` below): a single markdown `body`
  field, rendered client-side via the Blog/News CMS's existing sanitized
  `MarkdownContent` component/`react-markdown`+`remark-gfm`+`rehype-sanitize`
  pipeline — **no rich-text editor, no `rehype-raw`**, same "no new
  sanitization surface" convention `content_blog_post.body` already
  established. This schema layer has no sanitization responsibility of its
  own here — that is a frontend-render concern, identical division of labor
  to `content_blog_post.body`. `max_length=50000` copies
  `ContentBlogPostBase.body`'s exact budget (same class of content: a
  long-form plain-markdown text block, no reason to pick a different
  ceiling).
- `image` is new (`ImageContent` below) — genuinely new validation surface,
  not pure reuse, exactly as ADR-022's own "Trade-offs" section flags. See
  that model's own docstring for the full reasoning.

**`_PAGE_BLOCK_CONTENT_MODEL_BY_TYPE`** is this file's equivalent of
`homepage_section.py`'s `_CONTENT_MODEL_BY_TYPE` dispatch dict, mapping each
`PageBlockType` to its validated content sub-model.

**`PageBlock`** is the per-block validated unit (`type` + `content`), with
its own `model_validator(mode="after")` — the same per-type dispatch-and-
normalize pattern `HomepageSectionBase._validate_content_shape` uses, just
scoped to a single block rather than a whole document, because a page holds
an ordered *list* of these (`ContentPageBase.blocks`) rather than exactly one
per type. Malformed `content` for a given block's `type` is rejected by
Pydantic before it ever reaches Mongo, same guarantee the homepage sections
give per-document.

**`slug`** is the primary lookup key (`GET /v1/content/pages/{slug}`) and is
**immutable after creation**, same reasoning `ContentBlogPostUpdate`/
`ContentToolMetadataUpdate` omit `slug` entirely for —
`ContentPageUpdate` below has no `slug` field either. Reuses
`content_blog_post.py`'s exact `_SLUG_RE` regex/reasoning: a page slug is
admin-typed (not derived from an already-valid upstream source like
`content_tool_metadata`'s registry `href`), so it needs the same schema-layer
format check (`^[a-z0-9]+(-[a-z0-9]+)*$` — lowercase, hyphen-separated, no
leading/trailing/doubled hyphens, no uppercase, no whitespace, no Unicode).
Global uniqueness is still the DB index's job
(`content_pages_slug_unique`, see `database.py::ensure_indexes`) — this
validator only rejects malformed input before it gets that far.

**`RESERVED_TOP_LEVEL_SLUGS`** is the single source of truth for ADR-022's
reserved-slug blocklist (the frontend will separately mirror this as its own
constant for the `app/[slug]/page.tsx` catch-all's own defense-in-depth
check — that mirroring is a distinct future frontend task, not built here;
this is documented as the *canonical backend copy* that the service layer's
`SLUG_RESERVED` rejection must check against on every `POST`/slug-changing
`PATCH`, per ADR-022's decision). Seeded by directly inspecting
`frontend/app/`'s current top-level route folders (verified this session,
not assumed): `admin`, `api`, `blog`, `login`, `privacy`, `reset-password`,
`signup`, `terms`, `tools` — plus reserved framework/well-known names not
present as folders today but reserved regardless per ADR-022's own list:
`favicon.ico` (Next.js treats `app/favicon.ico` as a special-file route,
already present in `frontend/app/` today), `robots.txt`, `sitemap.xml`
(conventional well-known paths, not yet implemented as routes in this repo
per the Blog/News CMS's own deferred sitemap/robots trade-off, but reserved
now in case they are added later, per ADR-022's explicit inclusion).

**This is a point-in-time snapshot, not a live-derived list** — ADR-022's own
documented trade-off (see its "Trade-offs" section): if a future developer
adds a new top-level static route under `frontend/app/` (e.g. `/pricing`,
`/dashboard`) without updating this constant by hand, and an admin has
already published a dynamic page at that exact slug, Next.js's
static-over-dynamic route precedence means the new static route silently
wins — the previously-published CMS page stops being reachable through the
catch-all, with no error and no 404, just the wrong content served at that
URL from then on. A deploy-time check that diffs live `content_pages` slugs
against `frontend/app/`'s current top-level folder names would close this
gap, but is **explicitly deferred, not built in Round 1** — flagged as a
Next Action in both ADR-022 and the approved spec.

**`meta_description`** is `min_length=1, max_length=500` — same budget class
as `content_blog_post.excerpt`/`content_tool_metadata.description`, since it
serves the same job: an SEO `<meta name="description">` tag rendered by
`app/[slug]/page.tsx`'s `generateMetadata()`.

**`blocks`** requires at least one entry (`Field(..., min_length=1)`) — a
page with zero blocks isn't a real page, same "must have real content"
posture `ContentBlogPostBase.body`'s `min_length=1` enforces for blog posts,
just expressed at the list level here since a page's content is a list of
blocks rather than a single field.

**`status`**/**`published_at`** follow `content_blog_post.py`'s exact
contract — see that module's docstring for the full reasoning, repeated only
in summary here:

- `status` (`PageStatus`: `draft` | `published`) is required at every layer;
  `ContentPageCreate` overrides it with a default of `DRAFT` so a new page
  starts unpublished unless the admin explicitly requests
  `status="published"` at create time.
- `published_at` is server-stamped, never client-supplied. `ContentPageBase`
  and therefore `ContentPageCreate`/`ContentPageUpdate` **do not declare this
  field at all** — because `ConfigDict(extra="forbid")` is inherited, a
  request body containing a `published_at` key fails validation outright
  rather than being silently accepted or ignored. Recommendation for the
  service layer (backend-builder's next task, not built here): stamp
  `published_at = datetime.utcnow()` exactly once, on the first
  draft→published transition, and never overwrite it again afterward
  (including later unpublish/republish cycles) — identical semantics to
  `ContentBlogPostDocument.published_at`. `ContentPageDocument` is the only
  shape that carries this field (`Optional[datetime]`, `None` for any page
  that has never been published), deliberately absent from `Base` for the
  same "don't let `Create` accidentally inherit it as settable" reason
  `content_blog_post.py` documents.

**No `category`/`tags` fields** — out of scope per the approved spec. Dynamic
pages have neither a taxonomy nor a tagging concept; don't copy that part of
`content_blog_post.py`'s shape here.

**Indexing decisions** (see `app/core/database.py::ensure_indexes` for the
actual index creation):

- `slug` (unique, ascending): the primary lookup key for
  `GET /v1/content/pages/{slug}`, and doubles as the DB-layer backstop
  against a duplicate-slug create ever succeeding — same insurance role
  `content_categories_slug_unique`/`tags_slug_unique`/
  `content_tool_metadata_slug_unique`/`content_blog_posts_slug_unique` play
  for their own collections.
- **No `(status, published_at)` compound index, unlike `content_blog_posts`.**
  This is a deliberate divergence, not an oversight, and it rests on two
  independent legs rather than just "this collection will probably stay
  small":
    1. *Expected shape*: dynamic pages are marketing/landing/legal one-off
       pages (About, Careers, Terms-adjacent one-offs, campaign landing
       pages) — a bounded, low-cardinality set an admin curates by hand, not
       an ongoing editorial cadence that grows indefinitely the way blog
       posts do. Closer to `content_tool_metadata`'s "tiny collection, full
       scan is already sub-millisecond" reasoning than to
       `content_blog_posts`'s proactive-indexing case.
    2. *Actual query shape (the stronger argument)*: per the approved spec,
       the public read path is `GET /v1/content/pages/{slug}` — a
       `find_one({"slug": ..., "status": "published"})`, already served by
       the `slug` unique index above (a single-document point lookup; the
       `status` equality check on the one already-fetched document costs
       nothing extra). Unlike the blog index's public route
       (`find({"status": "published"}).sort("published_at", -1)` with
       pagination over a growing collection — the exact shape
       `content_blog_posts_status_published_at` exists to serve), nothing in
       the approved spec has this collection's admin list route filtering or
       sorting by `status`/`published_at` at all — it lists *all* pages
       (`list incl. drafts`), full stop. There is no sorted, filtered,
       paginated query anywhere in this feature that a `(status,
       published_at)` index would actually serve, so adding one would be
       pure write-side cost for zero read-side benefit — exactly what
       Handbook C.9's "index only fields that are actually queried" rule
       argues against.
    3. *Honest caveat*: unlike `content_tool_metadata` (hard-capped at the
       tool count — it structurally cannot outgrow the product's own tool
       catalogue), there is **no hard cap enforced anywhere** on how many
       dynamic pages an admin can create. If a future use case emerges that
       treats this collection as a bulk/programmatic-SEO landing-page
       generator rather than a handful of hand-curated one-offs, both legs
       of this reasoning could stop holding, and an admin-list filter/sort
       query could get added that this decision doesn't anticipate. Revisit
       then, same as `content_tool_metadata.py`'s own "revisit only if a
       category-filtered list query is added and the collection has grown
       enough for a scan to matter" posture.
- No TTL index: structural editorial content, no natural expiry (Handbook
  C.9's TTL rule doesn't apply) — a published or draft page persists
  indefinitely until an admin explicitly deletes it, same as every sibling
  collection in this feature family.
"""

import re
from datetime import datetime
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.schemas.common import PyObjectId
from app.schemas.homepage_section import AdSlotContent, BannerContent, BannerLink, HeroContent

# Re-exported for convenience/discoverability from this module too (backend-
# builder's routers/services may want to import banner/link shapes alongside
# page-specific ones without reaching into homepage_section.py directly).
__all__ = [
    "PageStatus",
    "PageBlockType",
    "RichTextContent",
    "ImageContent",
    "PageBlock",
    "RESERVED_TOP_LEVEL_SLUGS",
    "ContentPageBase",
    "ContentPageCreate",
    "ContentPageUpdate",
    "ContentPageDocument",
    "HeroContent",
    "AdSlotContent",
    "BannerContent",
    "BannerLink",
]

# Lowercase ASCII alphanumerics, hyphen-separated, no leading/trailing/doubled
# hyphens - identical to content_blog_post.py's _SLUG_RE, same reasoning: a
# page slug is admin-typed, not derived from an already-valid upstream source
# like content_tool_metadata's registry href.
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class PageStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


class PageBlockType(str, Enum):
    HERO = "hero"
    RICH_TEXT = "rich_text"
    IMAGE = "image"
    CTA_BANNER = "cta_banner"
    AD_SLOT = "ad_slot"


class RichTextContent(BaseModel):
    """A single markdown text block. Rendered client-side via the Blog/News
    CMS's existing sanitized `MarkdownContent` component
    (`react-markdown`+`remark-gfm`+`rehype-sanitize`, no `rehype-raw`) - no
    rich-text editor, no structured block format. This schema layer has no
    sanitization responsibility of its own - that is a frontend-render
    concern, same division of labor as `content_blog_post.body`. See module
    docstring."""

    model_config = ConfigDict(extra="forbid")

    body: str = Field(
        ...,
        min_length=1,
        max_length=50000,
        description=(
            "Plain markdown, no rich text editor or structured block format "
            "- same budget as ContentBlogPostBase.body. Rendered client-side "
            "via the existing sanitized MarkdownContent pipeline."
        ),
    )


class ImageContent(BaseModel):
    """New validation surface (ADR-022's own documented trade-off - the
    URL-scheme allowlist mirrors `ContentBlogPostBase._validate_cover_image_scheme`
    in shape and reasoning, but is new code, not a shared function today).

    `alt_text` is **required**, unlike `content_blog_post.cover_image_url`'s
    fully-optional field - an image block with no alt text is an
    accessibility/SEO regression this schema should refuse to allow, unlike
    `cover_image_url`, which is a secondary decorative field on a blog post
    that already has a title carrying the page's primary textual content. An
    `image` block here, by contrast, may be the only content in that
    position on the page - there is no guaranteed sibling text to fall back
    on, so alt text can't be optional."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(
        ...,
        min_length=1,
        max_length=2048,
        description=(
            "Plain admin-pasted URL to an externally-hosted image - not a "
            "file upload, same reasoning content_blog_post.cover_image_url "
            "documents for staying out of the Files/Job module's scope. "
            "Restricted to http(s):// - see the field validator below."
        ),
    )
    alt_text: str = Field(
        ...,
        min_length=1,
        max_length=300,
        description=(
            "Required, not optional - see class docstring. An image block "
            "with no alt text is an accessibility/SEO regression this "
            "schema refuses to allow."
        ),
    )
    caption: Optional[str] = Field(default=None, max_length=500)

    @field_validator("url")
    @classmethod
    def _validate_image_url_scheme(cls, value: str) -> str:
        # Copies ContentBlogPostBase._validate_cover_image_scheme's exact
        # logic and reasoning: not exploitable as script-executing XSS via
        # <img src> today (browsers don't execute a javascript:/data: URI
        # from that sink the way they would from an <a href>), but this
        # value isn't guaranteed to stay an <img src>-only sink forever (a
        # future reuse - a "share" link, an Open Graph <meta> tag - would
        # inherit an otherwise-silent gap), and this feature family's other
        # admin-authored-content fields already claim a defense-in-depth
        # posture this field should live up to too. Restricting to http(s)
        # here closes it at the one place every future consumer reads from.
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(
                f"url {value!r} must start with http:// or https:// - "
                "no other URL scheme is accepted"
            )
        return value


_PAGE_BLOCK_CONTENT_MODEL_BY_TYPE: dict[PageBlockType, type[BaseModel]] = {
    PageBlockType.HERO: HeroContent,
    PageBlockType.RICH_TEXT: RichTextContent,
    PageBlockType.IMAGE: ImageContent,
    PageBlockType.CTA_BANNER: BannerContent,
    PageBlockType.AD_SLOT: AdSlotContent,
}


class PageBlock(BaseModel):
    """A single ordered content block within a page's `blocks` list. Mirrors
    `HomepageSectionBase._validate_content_shape`'s per-type dispatch-and-
    normalize pattern, adapted to a per-block (not per-document) validator
    since a page holds a *list* of these rather than exactly one document
    per type - see module docstring."""

    model_config = ConfigDict(extra="forbid")

    type: PageBlockType = Field(..., description="Discriminates which sub-model `content` is validated against")
    content: dict[str, Any] = Field(
        default_factory=dict,
        description="Shape varies by `type` - validated against a per-type sub-model, see module docstring",
    )

    @model_validator(mode="after")
    def _validate_block_content_shape(self) -> "PageBlock":
        model_cls = _PAGE_BLOCK_CONTENT_MODEL_BY_TYPE[self.type]
        try:
            validated = model_cls(**self.content)
        except Exception as exc:
            raise ValueError(
                f"content does not match the '{self.type.value}' block shape: {exc}"
            ) from exc
        # Round-trip through the typed sub-model so defaults are applied and
        # storage always holds a normalized shape, same reasoning
        # HomepageSectionBase._validate_content_shape documents.
        self.content = validated.model_dump(exclude_none=True)
        return self


# Canonical backend copy of ADR-022's reserved-slug blocklist - see module
# docstring's "RESERVED_TOP_LEVEL_SLUGS" section for the full reasoning and
# the point-in-time-snapshot caveat. The service layer (backend-builder's
# next task, not built here) must reject any POST or slug-changing PATCH
# whose `slug` (case-sensitive - slugs are already lowercase-enforced by
# `_SLUG_RE`) is a member of this set, with a dedicated `SLUG_RESERVED`
# error code, per ADR-022's decision.
#
# Verified directly against `frontend/app/`'s actual top-level contents this
# session (not assumed): `admin`, `api`, `blog`, `favicon.ico`, `globals.css`,
# `layout.test.tsx`, `layout.tsx`, `login`, `page.test.tsx`, `page.tsx`,
# `privacy`, `reset-password`, `signup`, `terms`, `tools`. Of those,
# `admin`/`api`/`blog`/`login`/`privacy`/`reset-password`/`signup`/`terms`/
# `tools` are the actual route-owning folders; `favicon.ico` is Next.js's
# special-file convention for the `/favicon.ico` route (already present as a
# real file in `frontend/app/` today, unlike `robots.txt`/`sitemap.xml`,
# which are reserved pre-emptively per ADR-022's own list even though no
# `robots.ts`/`sitemap.ts` route exists in this repo yet - see the Blog/News
# CMS's own deferred sitemap/robots trade-off). `globals.css`/
# `layout.tsx`/`layout.test.tsx`/`page.tsx`/`page.test.tsx` are not routes
# (global layout/root page/test files, not folder-based route segments) and
# are correctly excluded.
RESERVED_TOP_LEVEL_SLUGS: frozenset[str] = frozenset(
    {
        "admin",
        "api",
        "blog",
        "login",
        "privacy",
        "reset-password",
        "signup",
        "terms",
        "tools",
        "favicon.ico",
        "robots.txt",
        "sitemap.xml",
    }
)


class ContentPageBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "Immutable after creation, admin-supplied directly (NOT derived "
            "from a registry). Must be lowercase, hyphen-separated "
            "(validated below), and must not be a member of "
            "RESERVED_TOP_LEVEL_SLUGS (enforced at the service layer, not "
            "this schema layer - see module docstring)."
        ),
    )
    title: str = Field(..., min_length=1, max_length=200)
    meta_description: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description=(
            "Rendered into <meta name='description'> by "
            "app/[slug]/page.tsx's generateMetadata() - same budget class "
            "as content_blog_post.excerpt/content_tool_metadata.description, "
            "since it serves the same SEO-meta-tag purpose."
        ),
    )
    blocks: list[PageBlock] = Field(
        ...,
        min_length=1,
        description=(
            "Ordered list of typed content blocks. A page with zero blocks "
            "isn't a real page - see module docstring."
        ),
    )
    status: PageStatus = Field(
        ...,
        description=(
            "Required at every layer. ContentPageCreate overrides this with "
            "a default of DRAFT; ContentPageUpdate exposes it as a plain "
            "optional field for publish/unpublish transitions."
        ),
    )

    @field_validator("slug")
    @classmethod
    def _validate_slug_shape(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                f"slug {value!r} must be lowercase, hyphen-separated "
                "(e.g. 'about-us') - no uppercase, whitespace, underscores, "
                "or leading/trailing/doubled hyphens"
            )
        return value


class ContentPageCreate(ContentPageBase):
    """Shape used when inserting a new `content_pages` document via the
    `require_admin`-gated `POST /v1/content/admin/pages` route
    (backend-builder's next step, not part of this task).

    `status` defaults to DRAFT here (overriding Base's required-with-no-
    default declaration) so a new page starts unpublished unless the admin
    explicitly requests `status="published"` at create time.

    `published_at` is deliberately NOT a field on this schema - see module
    docstring's `published_at` section. Because `ConfigDict(extra="forbid")`
    is inherited from `ContentPageBase`, a request body containing a
    `published_at` key fails validation outright rather than being silently
    accepted or ignored.

    Reserved-slug rejection (`SLUG_RESERVED` against
    `RESERVED_TOP_LEVEL_SLUGS`) is a service-layer concern, not enforced by
    this schema - see module docstring.
    """

    status: PageStatus = Field(default=PageStatus.DRAFT)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ContentPageUpdate(BaseModel):
    """Partial-update shape for admin edits. Plain `BaseModel`, not a
    subclass of `ContentPageBase` - same reasoning `ContentBlogPostUpdate`'s
    docstring documents: `slug`/`published_at`/`created_at`/`updated_at` are
    deliberately absent.

    - `slug` is absent because it's immutable after creation (same reasoning
      `ContentBlogPostUpdate`/`ContentToolMetadataUpdate` omit `slug` for).
      NOTE: per ADR-022's own scope, if a future slug-rename capability is
      ever added, it must re-run the same `SLUG_RESERVED` check a create
      does - not built here, flagged for backend-builder.
    - `published_at`/`created_at`/`updated_at` are absent because they are
      server-stamped only, never directly settable by a caller - see module
      docstring's `published_at` section.

    No `category`/`tags` fields at all (unlike `ContentBlogPostUpdate`) -
    dynamic pages have neither, out of scope per the approved spec.
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    meta_description: Optional[str] = Field(default=None, min_length=1, max_length=500)
    blocks: Optional[list[PageBlock]] = Field(default=None, min_length=1)
    status: Optional[PageStatus] = None


class ContentPageDocument(ContentPageBase):
    """Shape of a `content_pages` document as read back from MongoDB.

    `published_at` lives only here (and not on `Base`/`Create`/`Update`) -
    see module docstring's `published_at` section for why keeping it off
    `Base` matters (so `Create` doesn't inherit it as an accidentally
    settable field). The actual stamping logic (stamp once, on the first
    draft->published transition, never overwritten again) is a
    service-layer concern for backend-builder, not implemented here.
    """

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    published_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Server-stamped exactly once, on the first draft->published "
            "transition; never overwritten again afterward, including on "
            "later unpublish/republish cycles. None for any page that has "
            "never been published. See module docstring."
        ),
    )
    created_at: datetime
    updated_at: datetime
