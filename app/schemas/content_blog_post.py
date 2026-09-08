"""Pydantic models for the `content_blog_posts` collection.

New collection for the Blog/News CMS (`content` module, ADR-021's
already-built foundation — feature spec approved per
`docs/roadmap/SPRINT_STATUS.md`'s newest entries, direct extension of the
just-shipped Tools Metadata CMS, `app/schemas/content_tool_metadata.py`),
flagged per CLAUDE.md's "don't invent a new collection without flagging it"
rule, same convention `content_categories`/`tags`/`content_tool_metadata`
were flagged under (`app/core/database.py::ensure_indexes` has the running
list).

Backs full blog/news posts (title, category, tags, excerpt, body, cover
image, ad slot, publish status). Unlike `content_tool_metadata`, this
collection has no code-owned registry it decorates - a blog post is a
first-class content item that exists only in Mongo, with no
`tools-registry.ts`-equivalent source of truth to derive anything from.
That is the root cause of every real schema difference from
`content_tool_metadata.py` documented below; where this file doesn't call
out a difference, assume the same reasoning as that sibling applies
unchanged.

Collection: `content_blog_posts` (lowercase-plural, matches this repo's
other collections). Fields: snake_case, matching `content_categories`'/
`tags`'/`content_tool_metadata`'s convention within this same `content`-
module feature family (see `content_category.py`'s docstring for why this
diverges from Handbook Part C.9's camelCase default for `files`/`jobs`/etc).

**`slug`** is the primary lookup key (`GET /v1/content/blog-posts/{slug}`)
and is **immutable after creation**, same reasoning `ContentToolMetadataUpdate`
omits `slug` entirely for - `ContentBlogPostUpdate` below has no `slug`
field either.

Unlike `content_tool_metadata.slug` (which is derived from
`tools-registry.ts`'s `href` and therefore arrives already
lowercase-hyphenated by construction - see that module's docstring), a blog
post has no such upstream source: the admin types a title and a slug for a
brand-new post directly into a CMS form. Because nothing upstream of this
schema layer guarantees the shape, `slug` here gets a `field_validator`
tool-metadata never needed, rejecting anything that isn't lowercase
ASCII alphanumerics separated by single hyphens (`^[a-z0-9]+(-[a-z0-9]+)*$`
- no leading/trailing/doubled hyphens, no uppercase, no whitespace, no
Unicode). This is a schema-layer format check only; global uniqueness is
still enforced the same way as every sibling collection - the
`content_blog_posts_slug_unique` DB index (see `database.py::ensure_indexes`)
is the actual backstop, this validator only rejects malformed input before
it gets that far.

**`category`** stores a `content_categories.slug` string value - **this
schema layer intentionally does not query the database to validate it**,
same split `content_tool_metadata.category` uses. The one genuinely
different rule backend-builder must not miss: the service layer here must
resolve this value against `content_categories` filtered to
**`content_type="blog"`**, not `content_type="tool_metadata"`. The two CMS
systems share one `content_categories` collection (ADR-021) but validate
`category` against two disjoint, non-overlapping slices of it - a blog post
whose `category` matches a `tool_metadata` category slug (or vice versa)
must still be rejected, because ADR-021 explicitly keeps the two taxonomies
independent (tool categories are code-owned and read-only; blog categories
are the only genuinely mutable rows). Copying `content_tool_metadata_service.py`'s
category-resolution call without changing its `content_type` filter
argument would silently accept blog posts filed under tool categories (or
reject every legitimate blog category) - called out here explicitly so this
mistake doesn't happen by pattern-matching habit.

**`tags`** stores canonical tag *slugs* only - **never raw admin-typed
strings** - identical contract to `content_tool_metadata.tags`: the service
layer must run every incoming raw tag string through
`app.services.content.tags_service.get_or_create_tag` first and store only
the returned `TagDocument.slug` values here. This schema layer does not
itself normalize or validate against `db.tags`.

**`excerpt` vs `body`** is a genuinely new split versus `content_tool_metadata`,
which gets by with a single `description` field (max 500 chars) serving both
the tool-card teaser and the on-page copy, because a tool's marketing copy is
short enough that one field can do both jobs. A blog post can't reuse that
shape: the blog index page needs a short teaser (and the post page needs a
short `<meta name="description">`) that is deliberately *not* the full
article, while the post page itself needs the complete, much longer article
body. Conflating them into one field would force a bad trade-off - either
cap the whole article at ~500 chars (unusable for a real post) or let the
index/meta-description card render an entire multi-thousand-word article
(broken UX and bad SEO, since search engines truncate long meta
descriptions arbitrarily). So two fields, two purposes, two length budgets:
- `excerpt`: `max_length=500` - same budget as `content_tool_metadata.description`,
  since it serves the same two jobs (index-card teaser + meta description)
  for the same reasons.
- `body`: `max_length=50000` - generous headroom for a genuinely long-form
  post (roughly 8,000-10,000 words of plain markdown), well beyond what any
  realistic blog post on this site is expected to need, while still being a
  hard ceiling rather than unbounded (Handbook engineering-rules default of
  never leaving a string field unbounded).

Both fields are plain markdown strings, no rich-text editor and no
structured block format - same "no rich text editor" convention
`content_tool_metadata.py`'s docstring documents for `how_to_use`/`faq`,
continued here rather than revisited. That file's docstring already
anticipated this: "the separate, still-queued Blog/News CMS may need a
richer editor later; not anticipated here" - this task doesn't reopen that
question either; plain markdown remains the answer until a future task
deliberately decides otherwise.

**`cover_image_url`** is a plain admin-pasted URL string
(`Optional[str]`, `max_length=2048` - the de facto browser/URL-length
convention, generous for any realistic image CDN/hotlink URL), **not a file
upload**. This deliberately keeps the Blog/News CMS out of the Files/Job
module's scope: uploading, storing, and serving an actual image file
through this codebase's existing upload pipeline (`files`/`jobs` modules,
local VPS storage + `storagePath` pattern per Handbook C.9) is a materially
larger scope than a Tier-1 CRUD text-and-metadata feature needs, and would
duplicate machinery this feature doesn't otherwise touch. This mirrors how
`AdSlotContent` (reused verbatim below) already avoids inventing new
file-handling for ad creative - both fields assume the actual asset is
hosted elsewhere (an existing image CDN, or manually uploaded via the VPS
file storage pattern by an operator, out of band) and this schema only ever
stores a reference URL to it, never bytes. If an in-CMS image upload flow is
ever wanted, that is a distinct future feature spec that would route
through `files`/`jobs` properly - not silently bolted on here.

Restricted to `http://`/`https://` (validator below, duplicated on both
`ContentBlogPostBase` and `ContentBlogPostUpdate` since the latter doesn't
inherit from the former - see that class's docstring). `security-reviewer`
flagged (Low) that an unconstrained string here isn't exploitable as
script-executing XSS via `<img src>` today (browsers don't execute a
`javascript:`/`data:` URI from that sink the way they would from an
`<a href>`), but the field wasn't guaranteed to stay an `img src`-only sink
forever, and the schema's other admin-authored-content fields already claim
a defense-in-depth posture this field didn't live up to. Fixed at the
schema layer so every future consumer of this field inherits the
restriction rather than needing to re-check it themselves.

**`status`** (`BlogPostStatus`: `draft` | `published`) is a required field
at every layer (a document always has *some* status), but `ContentBlogPostCreate`
overrides it with a default of `BlogPostStatus.DRAFT` so a new post starts
unpublished unless an admin explicitly sets `status="published"` at create
time; `ContentBlogPostUpdate` exposes it as a plain optional field so an
admin can flip a draft to published (or unpublish) via a normal edit.

**`published_at`** is the one field on this schema that needs the most
deliberate judgment call, flagged here explicitly for backend-builder:

- It is **server-stamped, never client-supplied** - `ContentBlogPostCreate`
  and `ContentBlogPostUpdate` **do not declare this field at all** (not even
  as an ignorable optional the client could set). Because both schemas
  inherit `ConfigDict(extra="forbid")`, a request body that includes a
  `published_at` key fails validation outright with a clear Pydantic error,
  rather than silently accepting-and-ignoring it (which `extra="ignore"`
  would do) or silently trusting a client-chosen timestamp (which declaring
  it as a normal optional field would do). This is the same "fail loud,
  never silently no-op or coerce" convention this feature family already
  uses for `CategoryReadOnly`/`ToolGridContent`'s closed schema/etc.
- Recommendation for the service layer (backend-builder's next task, not
  built here): stamp `published_at = datetime.utcnow()` exactly once, at the
  moment a post's `status` transitions from `draft` to `published` for the
  *first* time, and never overwrite it again afterward - including if the
  post is later unpublished and republished. This is a product judgment
  call this schema file cannot itself enforce (it requires comparing the
  existing document's prior status to the incoming update, which is
  inherently a service-layer, not schema-layer, concern), but is documented
  here so backend-builder doesn't have to re-derive the intent from
  scratch. The reasoning: `published_at` should answer "when did this post
  first go live" for chronological sorting/display purposes (the public
  list query sorts by it - see the indexing section below) - letting every
  unpublish/republish cycle bump it would make that sort order misleading
  and would let an admin quietly "bump" an old post back to the top of the
  blog index by toggling status off and on, which is very likely not an
  intended feature.
- `ContentBlogPostDocument` is the only shape that carries this field (as
  `Optional[datetime]`, `None` for any post that has never been published).
  It deliberately is **not** part of `ContentBlogPostBase`, specifically so
  that `ContentBlogPostCreate` (which inherits from `Base`) doesn't
  accidentally expose it as a settable field through inheritance - it's
  added only on `Document`, the DB-read shape, mirroring how
  `created_at`/`updated_at` are likewise absent from `Base` and added
  individually on `Create` (as caller-defaulted-but-settable) and `Document`
  (as always-present) rather than shared upward.

**`ad_slot`** reuses `AdSlotContent` (`placement_id`, `height_px`) from
`app/schemas/homepage_section.py` directly, imported the same way
`content_tool_metadata.py` does - not redefined here, so the three features
sharing this shape can never drift on it.

**Indexing decisions** (see `app/core/database.py::ensure_indexes` for the
actual index creation):

- `slug` (unique, ascending): the primary lookup key for
  `GET /v1/content/blog-posts/{slug}`, and doubles as the DB-layer backstop
  against a duplicate-slug create ever succeeding - same insurance role
  `content_categories_slug_unique`/`tags_slug_unique`/
  `content_tool_metadata_slug_unique` play for their own collections.
- `(status, published_at)` compound index, descending on `published_at`:
  **this is the one place this collection's indexing story deliberately
  diverges from every sibling in this feature family.** `content_categories`
  stays at ~10-20 rows forever (a fixed taxonomy), `tags` is unbounded but
  never queried by anything except a full-scan autocomplete list (cheap
  regardless of size), and `content_tool_metadata` is hard-capped at the
  tool count (~57, one row per tool - it literally cannot grow faster than
  the product's own tool catalogue does). `content_blog_posts` has no such
  ceiling: it's the first collection in this feature family actually
  expected to grow unboundedly over the site's lifetime as an ordinary
  editorial cadence adds posts indefinitely. The public blog index's real
  query shape is `find({"status": "published"}).sort("published_at", -1)`
  with pagination (a separate backend-builder task is adding a generic
  pagination utility, not built here) - exactly the query this compound
  index is built to serve efficiently at any collection size, rather than
  relying on the "tiny collection, full scan is already sub-millisecond"
  reasoning `content_tool_metadata_service`/`content_categories_service`
  correctly rely on for their own bounded collections. Compare directly
  with `content_tool_metadata.py`'s "No index on category... at most ~57
  documents... revisit only if a category-filtered list query is added and
  the collection has grown enough for a scan to matter" - this collection
  makes the opposite call proactively, precisely because the growth pattern
  that reasoning depends on not happening is exactly what's expected to
  happen here.
- No TTL index: structural editorial content, no natural expiry (Handbook
  C.9's TTL rule doesn't apply) - a published or draft post persists
  indefinitely until an admin explicitly deletes it, same as every sibling
  collection in this feature family.
"""

import re
from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.common import PyObjectId
from app.schemas.homepage_section import AdSlotContent

# Lowercase ASCII alphanumerics, hyphen-separated, no leading/trailing/doubled
# hyphens - see module docstring for why this needs schema-layer enforcement
# here (a blog slug is admin-typed, not derived from an already-valid
# upstream source like content_tool_metadata's registry href).
_SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


class BlogPostStatus(str, Enum):
    DRAFT = "draft"
    PUBLISHED = "published"


class ContentBlogPostBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "Immutable after creation, admin-supplied directly (NOT derived "
            "from a registry - see module docstring). Must be lowercase, "
            "hyphen-separated (validated below)."
        ),
    )
    title: str = Field(..., min_length=1, max_length=200)
    category: str = Field(
        ...,
        min_length=1,
        max_length=100,
        description=(
            "A content_categories.slug value. NOT validated against the "
            "database at this schema layer - the service layer must resolve "
            "this against content_categories filtered to "
            "content_type='blog' (NOT 'tool_metadata' - see module "
            "docstring for why this distinction matters) and reject the "
            "write otherwise."
        ),
    )
    tags: list[str] = Field(
        default_factory=list,
        description=(
            "Canonical tag slugs only - never raw strings. The service layer "
            "must run every incoming raw tag through "
            "app.services.content.tags_service.get_or_create_tag before "
            "storing its slug here. See module docstring."
        ),
    )
    excerpt: str = Field(
        ...,
        min_length=1,
        max_length=500,
        description=(
            "Short teaser for the blog index card and <meta "
            "name='description'> - NOT the full post. See module docstring "
            "for why this is separate from `body`."
        ),
    )
    body: str = Field(
        ...,
        min_length=1,
        max_length=50000,
        description=(
            "Full post content, plain markdown - no rich text editor or "
            "structured block format (see module docstring)."
        ),
    )
    cover_image_url: Optional[str] = Field(
        default=None,
        max_length=2048,
        description=(
            "Plain admin-pasted URL to an externally-hosted image - not a "
            "file upload. See module docstring for why this stays out of "
            "the Files/Job module's scope. Restricted to http(s):// - see "
            "the field validator below."
        ),
    )
    status: BlogPostStatus = Field(
        ...,
        description=(
            "Required at every layer. ContentBlogPostCreate overrides this "
            "with a default of DRAFT; ContentBlogPostUpdate exposes it as a "
            "plain optional field for publish/unpublish transitions."
        ),
    )
    ad_slot: Optional[AdSlotContent] = Field(
        default=None,
        description="Reuses AdSlotContent (placement_id, height_px) from homepage_section.py verbatim.",
    )

    @field_validator("slug")
    @classmethod
    def _validate_slug_shape(cls, value: str) -> str:
        if not _SLUG_RE.match(value):
            raise ValueError(
                f"slug {value!r} must be lowercase, hyphen-separated "
                "(e.g. 'my-blog-post') - no uppercase, whitespace, "
                "underscores, or leading/trailing/doubled hyphens"
            )
        return value

    @field_validator("cover_image_url")
    @classmethod
    def _validate_cover_image_scheme(cls, value: Optional[str]) -> Optional[str]:
        # security-reviewer finding (Blog/News CMS review): this field was
        # previously an unconstrained string, accepted verbatim and rendered
        # into `<img src>` on the public blog pages with no scheme check.
        # Not exploitable as script-executing XSS today (browsers don't
        # execute a `javascript:`/`data:` URI from an `<img src>` the way
        # they would from an `<a href>`), but the field's own docstring
        # already claims a defense-in-depth posture for admin-authored
        # content, and this value isn't guaranteed to stay an `img src`-only
        # sink forever (a future reuse - a "share" link, an Open Graph
        # `<meta>` tag - would inherit an otherwise-silent gap). Restricting
        # to http(s) here closes it at the one place every future consumer
        # reads from, rather than requiring every future consumer to
        # remember to re-check it themselves.
        if value is None:
            return value
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(
                f"cover_image_url {value!r} must start with http:// or https:// - "
                "no other URL scheme is accepted"
            )
        return value


class ContentBlogPostCreate(ContentBlogPostBase):
    """Shape used when inserting a new `content_blog_posts` document via the
    `require_admin`-gated `POST /v1/content/blog-posts` route
    (backend-builder's next step, not part of this task).

    `status` defaults to DRAFT here (overriding Base's required-with-no-
    default declaration) so a new post starts unpublished unless the admin
    explicitly requests `status="published"` at create time.

    `published_at` is deliberately NOT a field on this schema - see module
    docstring's `published_at` section. Because `ConfigDict(extra="forbid")`
    is inherited from `ContentBlogPostBase`, a request body containing a
    `published_at` key fails validation outright rather than being silently
    accepted or ignored.
    """

    status: BlogPostStatus = Field(default=BlogPostStatus.DRAFT)
    created_at: datetime = Field(default_factory=datetime.utcnow)
    updated_at: datetime = Field(default_factory=datetime.utcnow)


class ContentBlogPostUpdate(BaseModel):
    """Partial-update shape for admin edits. `slug` is deliberately absent -
    immutable after creation, same reasoning `ContentToolMetadataUpdate`
    omits `slug` for. `published_at` is also deliberately absent - see
    module docstring's `published_at` section: the service layer computes it
    exactly once (on the first draft->published transition) and it is never
    directly settable by a caller, same reasoning `ContentToolMetadataUpdate`
    gives for why `updated_at` is stamped by the service layer rather than
    accepted from the caller. `created_at`/`updated_at` are likewise absent
    for the same reason.

    `category`/`tags` still go through the same validation split as create
    (DB-resolution against `content_categories` filtered to
    `content_type='blog'`, and tag normalize-and-upsert, both stay
    service-layer concerns) - the service layer is expected to merge this
    onto the existing document and re-validate through
    `ContentBlogPostBase`'s rules before writing, same convention
    `ContentToolMetadataUpdate`'s docstring documents.
    """

    model_config = ConfigDict(extra="forbid")

    title: Optional[str] = Field(default=None, min_length=1, max_length=200)
    category: Optional[str] = Field(default=None, min_length=1, max_length=100)
    tags: Optional[list[str]] = None
    excerpt: Optional[str] = Field(default=None, min_length=1, max_length=500)
    body: Optional[str] = Field(default=None, min_length=1, max_length=50000)
    cover_image_url: Optional[str] = Field(default=None, max_length=2048)
    status: Optional[BlogPostStatus] = None
    ad_slot: Optional[AdSlotContent] = None

    # Not inherited from ContentBlogPostBase - ContentBlogPostUpdate is a
    # plain BaseModel, not a subclass (see class docstring for why: slug/
    # published_at/created_at/updated_at are deliberately absent, which is
    # easier to express as a separate model than as an override-and-delete
    # on Base). The cover_image_url scheme check must therefore be
    # duplicated here rather than relied on via inheritance - same
    # http(s)-only rule and reasoning as ContentBlogPostBase's validator.
    @field_validator("cover_image_url")
    @classmethod
    def _validate_cover_image_scheme(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return value
        if not (value.startswith("http://") or value.startswith("https://")):
            raise ValueError(
                f"cover_image_url {value!r} must start with http:// or https:// - "
                "no other URL scheme is accepted"
            )
        return value


class ContentBlogPostDocument(ContentBlogPostBase):
    """Shape of a `content_blog_posts` document as read back from MongoDB.

    `published_at` lives only here (and not on `Base`/`Create`/`Update`) -
    see module docstring's `published_at` section for why keeping it off
    `Base` matters (so `Create` doesn't inherit it as an accidentally
    settable field).
    """

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    published_at: Optional[datetime] = Field(
        default=None,
        description=(
            "Server-stamped exactly once, on the first draft->published "
            "transition; never overwritten again afterward, including on "
            "later unpublish/republish cycles. None for any post that has "
            "never been published. See module docstring."
        ),
    )
    created_at: datetime
    updated_at: datetime
