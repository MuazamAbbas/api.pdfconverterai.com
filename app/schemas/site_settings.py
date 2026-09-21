"""Pydantic models for the `site_settings` collection.

New collection for **Admin-managed SEO & site-verification settings, Round 1
(ads.txt + verification codes)** (`content` module, sibling feature to
`content_categories`/`tags`/`content_tool_metadata`/`content_blog_posts`/
`content_pages` — ADR-021's module boundary, reuses `require_admin` same as
those — founder-approved spec, `docs/roadmap/SPRINT_STATUS.md`'s 2026-09-19
"Spec approved: Admin-managed SEO & site-verification settings (Round 1: ads.txt
+ verification codes)" entry), flagged per CLAUDE.md's "don't invent a new
collection without flagging it" rule, same convention `content_categories`/
`tags`/`content_tool_metadata`/`content_blog_posts`/`content_pages`/
`analytics_counters` were flagged under (`app/core/database.py::ensure_indexes`
has the running list/comment for every one of them — see that file for why
`site_settings` deliberately gets a comment there with **no** `create_index`
call, unlike every sibling in that list).

**Not the same collection as Handbook Part C.9's `system_settings`.** That
name appears in the Handbook's canonical six-collection list but has never
been implemented as an actual `db.system_settings` collection anywhere in
this codebase (confirmed — the only two hits for the string
`"system_settings"` in `backend/` are `usage_limits.py`'s docstrings for
`ai_tools_usage`/`seo_tools_usage` *naming* it as one of the six canonical
collections those new collections are flagged against, not an actual
collection in use). `site_settings` is a genuinely new, distinct name for
this feature — chosen directly in the founder-approved spec, not a rename or
repurposing of `system_settings`.

## Why this collection is a true singleton, not a list

Unlike every other collection in this feature family (`content_categories`,
`tags`, `content_tool_metadata`, `content_blog_posts`, `content_pages`, and
`homepage_sections` before them) — all of which hold zero-to-many documents
looked up by `slug`/`type`/`_id` — `site_settings` holds **exactly one
document, forever**. There is only ever one ads.txt file and one set of
verification codes for the whole site; there is no plural concept here at
all, so a list-shaped collection (even one enforced to length 1 via a unique
index, the way `homepage_sections_type_tool_grid_unique` enforces "at most one
`tool_grid` document") would be modeling the wrong thing and would force
every future field addition (Round 2's `head_injection_code`/
`body_injection_code`) into the same "there happens to be only one" pattern
rather than what it actually is: one fixed, well-known settings document.

**Singleton mechanism — a fixed, well-known `_id` value:**

`SITE_SETTINGS_SINGLETON_ID = "site_settings"` (a plain string, not an
`ObjectId`/`PyObjectId`) is the literal, hardcoded primary key of the one
document this collection will ever contain. This is a deliberate, documented
divergence from every other collection's `PyObjectId`-aliased `_id` in this
codebase (`HomepageSectionDocument.id`, `ContentPageDocument.id`, etc.) —
those need an opaque, auto-generated identity because they address one of
*many* rows; this collection's "identity" is structural (there is exactly one
of it), so giving it a self-describing literal string key is clearer than an
arbitrary `ObjectId` and, more importantly, lets every read/write in the
service layer skip searching/listing entirely:

- **Read** (`GET /v1/content/site-settings`, public, unauthenticated, same
  posture as `GET /v1/content/categories`): the service layer always does
  `db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})` — a single
  point lookup by primary key, never a `find()`/list/sort of any kind.
- **Write** (`PUT /v1/content/site-settings`, admin, `require_admin`): the
  service layer always does
  `db.site_settings.update_one({"_id": SITE_SETTINGS_SINGLETON_ID}, {"$set": ...,
  "$setOnInsert": {"created_at": ...}}, upsert=True)` — again addressed
  directly by the known key, `upsert=True` so the very first admin write ever
  made both creates and updates the one document in a single atomic
  operation, with no separate "does it exist yet" branch needed.

**Seedless by design (spec acceptance criterion 3):** this collection is
deliberately allowed to not exist at all — no document, not even the
singleton one — from a brand-new deploy until the first admin `PUT`. Unlike
`homepage_sections`' `tool_grid` document (which the frontend homepage
*requires* to render and therefore must be pre-seeded by
`scripts/seed_homepage_sections.py` before the site works), nothing about the
public `/ads.txt` route or the root-layout verification-meta wiring
*requires* a document to already exist — an unconfigured site should just
serve an empty `ads.txt` and emit no verification meta tags, not 404 or
error. Concretely: the service layer's read path must treat "no document
found" as "return the all-empty defaults below," never as a not-found error
condition, and must **never** auto-insert a document as a side effect of a
`GET` (that would silently convert an unauthenticated public read into a
write path — a `GET` must stay side-effect-free). `default_site_settings()`
below exists to hand the service layer that exact all-empty defaults object
without it having to duplicate field defaults inline:

```python
doc = await db.site_settings.find_one({"_id": SITE_SETTINGS_SINGLETON_ID})
if doc is None:
    return default_site_settings()
return SiteSettingsRead(**SiteSettingsDocument(**doc).model_dump(exclude={"id", "created_at", "updated_at"}))
```

**Do not unpack a raw Mongo document directly into `SiteSettingsRead(**doc)`.** A real
document always also carries `_id`/`created_at`/`updated_at`, and `SiteSettingsRead`
inherits `extra="forbid"` from `SiteSettingsBase` — it doesn't declare those fields, so
unpacking the raw dict raises a validation error on every document that actually exists
(caught by `test-runner` before merge: every `GET` after any write, and the write path's
own re-read, 500'd). Validate the raw doc through `SiteSettingsDocument` first (which does
declare those fields), then narrow to just the `SiteSettingsBase` fields for the response shape —
see `app/services/content/site_settings_service.py`'s `_to_read()` helper, the one place
this pattern is actually implemented.

## Round 2 (implemented): raw `<head>`/`<body>` code injection

Round 2 (ADR-024, "Admin Custom Code Injection trust boundary" — now
Approved, continuing from ADR-023) adds `head_injection_code`/
`body_injection_code` fields to this *same* singleton document — deliberately
not a new collection, since it's the same one "site settings" object growing
new fields, and Mongo documents don't need a migration to gain new optional
fields on existing rows. `_id`, `created_at`/`updated_at`, and the singleton
read/write mechanism above are all field-count-agnostic and needed no changes
for this. The two new fields are declared directly on `SiteSettingsBase` (so
`SiteSettingsUpdate`/`Document`/`Read` all inherit them automatically, same as
every other field here) — no separate model/collection/endpoint, no new
route: the existing `GET`/`PUT /v1/content/site-settings` routes carry the
full shape once the schema gains the fields.

Per ADR-024's Decision: **no sanitization pipeline runs on these two fields,
at any layer, ever** — ADR-024 places the trust boundary at `require_admin`
itself (an admin account is already fully trusted, the same posture as
letting an admin edit `ads_txt_content` above), not at a content-sanitization
layer. The frontend renders both fields verbatim (`head_injection_code` via
`next/script`/raw head injection, `body_injection_code` via raw body
injection) — see ADR-024 for the full trust-boundary reasoning and why a
`<script>`-stripping/allowlist pipeline was explicitly rejected as
incompatible with the feature's own purpose (real GTM/analytics snippets are
always `<script>` tags). No field, validator, or docstring in this file
assumes "exactly these four fields" as a closed set; a future Round 3 would
follow this same precedent.

## Field caps and why

- `ads_txt_content: str`, capped at `max_length=50000` (~50KB), no
  `min_length` (empty string is the valid, expected "not configured yet"
  default — see `default_site_settings()`). Per the approved spec: a real
  multi-ad-network `ads.txt` file (Google AdSense plus several resold/backup
  networks, which is exactly the AdSense-continuity scenario this feature
  exists for) can run to a few hundred lines; each line is short
  (`domain.com, pub-XXXXXXXXXXXXXXXX, DIRECT, hash` is well under 100 chars),
  so even a few hundred lines comfortably fits inside 50KB with generous
  headroom — deliberately not a short-string field the way e.g.
  `ContentToolMetadataBase`'s title/description fields are, because this is
  a small full-text-file field, not a short label. Not sanitized/validated as
  HTML here: this value is served verbatim as `Content-Type: text/plain` by
  the frontend's `app/ads.txt/route.ts` (per the approved spec), never
  rendered into an HTML page, so there is no HTML-injection surface for this
  field to guard against at the schema layer — a materially different
  posture from `RichTextContent.body`/`ContentBlogPostBase.body` (which *are*
  rendered into HTML client-side via a sanitized markdown pipeline). If a
  future consumer ever renders this value inside an HTML page instead of
  serving it as a raw text file, that consumer becomes responsible for its
  own escaping at that point — flagged here so it isn't silently assumed.
- `SiteVerificationCode.name`/`.content`, each `min_length=1, max_length=200`.
  200 chars is generous relative to every real verification code observed in
  practice (Google/Bing/Yandex site-verification tokens are all well under 60
  chars, and `name` is always a short literal meta-tag name like
  `google-site-verification`/`msvalidate.01`/`yandex-verification`) while
  still bounded, not a short-label-sized field like most other `name`-ish
  fields elsewhere in this codebase (e.g. `BannerLink.label` at 100) nor an
  unbounded one. `min_length=1` (plus the control-character/whitespace-only
  guard below) exists because an empty or blank `name` would render as a
  broken `<meta name="" content="...">` tag via Next.js's
  `Metadata.verification.other` — a cheap, worthwhile schema-layer guard
  against a malformed but "successfully saved" entry.
- `verification_codes: list[SiteVerificationCode]`, capped at
  `max_length=50` (`default_factory=list`, so the "no codes configured yet"
  default is `[]`, not `None`). 50 is copied from
  `ContentPageBase.blocks`'s exact same "bound-by-round-number, generous
  headroom over the realistic count" convention and reasoning: the spec's
  three acceptance-test providers (Google/Bing/Yandex) is the realistic
  count, but there's no hard reason to hardcode "exactly 3" when
  "provider-agnostic by design" is the feature's own stated intent (any
  future verification provider is just another `{name, content}` entry) —
  50 gives generous headroom for that without leaving the list truly
  unbounded, which would otherwise let a single admin mistake (or a future
  bug in whatever writes this collection) produce an arbitrarily large
  document served unauthenticated by the public `GET` route with no size
  ceiling — the same "unbounded document field array" risk
  `content_page.py`'s `blocks` cap and its cited security-reviewer finding
  already establish the precedent for guarding against in this codebase.

`SiteVerificationCode.name`/`.content` also reject values that are empty or
whitespace-only after stripping, or that contain control characters
(`\\n`/`\\r`/`\\t`/other non-printable bytes) — the equivalent, for a field
rendered as an HTML *attribute* value, of `BannerLink.href`'s/
`ImageContent.url`'s scheme validators for fields rendered as HTML *URLs*:
cheap, schema-layer defense-in-depth against a malformed-but-technically-
non-empty value reaching Mongo, even though Next.js/React's default JSX
attribute rendering already HTML-escapes the value at render time (so this is
not closing an XSS gap the way those two URL validators are — there is no
known injection vector here today — it is purely a data-quality guard against
copy-paste mistakes, e.g. an admin accidentally including a trailing newline
from clipboard content).

- `head_injection_code`/`body_injection_code: str`, each capped at
  `max_length=20000` (~20KB), no `min_length` (empty string is the valid,
  expected "not configured yet" default, same posture as `ads_txt_content`
  above). A real GTM/analytics/pixel snippet is typically well under 5KB, so
  20KB is deliberately generous headroom over the realistic case — sized
  consistently with `ads_txt_content`'s own "generous, not tight" cap
  philosophy rather than a short-string field, while still bounded for the
  same "no unbounded document field served unauthenticated-read/
  authenticated-write with no size ceiling" reasoning `verification_codes`'
  cap above and `content_page.py`'s `blocks` cap both already establish.
  **No control-character rejection the way `SiteVerificationCode.name`/
  `.content` get**: those are single-line HTML-attribute values where a
  stray newline is a copy-paste mistake; these two fields are deliberately
  multi-line code (real `<script>` snippets have newlines/tabs throughout),
  so reusing `_reject_blank_or_control_chars` here would reject every
  legitimate value. Instead, `_reject_blank_or_control_chars_only_when_nonempty`
  (below) only rejects a *non-empty* value that is nothing but whitespace/
  control characters end-to-end (e.g. a stray pasted newline with no real
  code) — the empty-string default itself is always valid and never
  rejected, per ADR-024's Decision section ("may still cap length and reject
  blank/control-character-only values" — read literally: blank-or-control-
  character-*only*, not "empty", and not "contains any control character").
  **No HTML/script sanitization of any kind runs on either field, at any
  layer** — see this module's "Round 2 (implemented)" section above and
  ADR-024 directly for why that is the deliberate, approved design, not an
  oversight.

## Indexing decision: **no index beyond the default `_id` index**

Confirmed, not assumed: MongoDB automatically creates and unconditionally
maintains a unique index on every collection's `_id` field — this exists the
moment the collection exists, with zero code required. Since both the read
path and the write path above *always* address the single document directly
by its known `_id` (`SITE_SETTINGS_SINGLETON_ID`), that automatic index is
already the fastest possible access path (a point lookup by primary key) —
there is no secondary field this collection is ever filtered/sorted by (no
`slug`, no `status`, no `order`; the whole collection is one document with a
handful of top-level fields), so there is nothing left for a custom
`db.site_settings.create_index(...)` call to usefully index. This is a
sharper version of `content_tool_metadata.py`'s/`content_page.py`'s "tiny
collection, index only what's actually queried" reasoning — not just tiny,
but *exactly one document, addressed only by its own primary key*, which is
the one case in this codebase's collection family where the honest answer is
"zero additional indexes," not "a cheap unique index anyway for insurance."
`app/core/database.py::ensure_indexes()` gets a documentation-only comment
block for `site_settings` (matching every sibling collection's flagging
convention there) but deliberately **no** `create_index` call to go with it —
see that file.

No TTL index: structural site configuration, no natural expiry — same
reasoning as every other structural-content collection in this feature family
(`content_categories`/`content_tool_metadata`/`content_blog_posts`/
`content_pages`/`homepage_sections`).

## Shape notes

Deliberately does **not** follow the `Base`/`Create`/`Update`/`Document`
four-way split `homepage_section.py`/`content_page.py` use for list-backed
collections — there is no `Create` shape here at all, because nothing ever
inserts a *new* `site_settings` document through an explicit create route;
the one document's insert is an implicit, transparent side effect of the
first `PUT`'s `upsert=True` (see "Singleton mechanism" above), not a distinct
API operation with its own request shape. What this file has instead:

- `SiteSettingsBase` — the two Round 1 fields plus Round 2's
  `head_injection_code`/`body_injection_code`, shared by every other shape
  below. `extra="forbid"`, matching this codebase's default convention on
  every other schema in this feature family; a future Round 3 would add its
  fields here the same way, not as accepted-but-unvalidated extras.
- `SiteSettingsUpdate(SiteSettingsBase)` — the `PUT /v1/content/site-settings`
  request body. Round 2's `head_injection_code`/`body_injection_code` are
  **required** (overridden without defaults on this class, so an omitted
  field is a 422 rather than silently wiping stored code); the Round 1
  fields keep their Base defaults. No `Optional`/partial-update split the way
  `HomepageSectionUpdate` offers — a deliberate, simpler choice than a
  partial-update model:
  `PUT` on a singleton settings resource is naturally a full-replace
  operation (the admin settings page's form always holds every field at
  once, since there's only one such page and one such document), so the
  service layer never needs partial-merge logic — it can always
  `$set` every field directly. If a future UI need ever wants to edit these
  fields independently without resending the others, that would justify
  revisiting this as an `Optional`-partial model (or separate `PATCH`-style
  endpoints) — not needed for Round 1's or Round 2's spec as approved.
- `SiteSettingsDocument(SiteSettingsBase)` — the real, persisted Mongo
  document shape: adds `id: str` (aliased `_id`, **not** `PyObjectId` — see
  "Singleton mechanism" above for why this field is a plain string here
  unlike every sibling collection's `PyObjectId`), `created_at`, `updated_at`.
  Only ever instantiated from an actual `find_one` result — i.e. only when a
  document already exists.
- `SiteSettingsRead(SiteSettingsBase)` — the API response shape for both the
  public `GET` and the admin `PUT`'s response, deliberately just the
  `SiteSettingsBase` fields with no `id`/`created_at`/`updated_at` exposed.
  Neither route's approved spec/acceptance-criteria call for exposing
  document metadata, and the singleton's identity is structural/well-known
  anyway (no caller ever needs `_id` to address it — see above), so there's
  nothing useful `id` would tell an API caller. If a future admin-UI need
  wants to show a "last updated" timestamp, the admin route can build that
  response directly from a `SiteSettingsDocument` instead (`updated_at`
  already lives there) without any schema change here — flagged for
  backend-builder, not built now since it's outside the approved spec's
  acceptance criteria.
- `default_site_settings()` — returns
  `SiteSettingsRead(ads_txt_content="", verification_codes=[],
  head_injection_code="", body_injection_code="")`, the sane empty-defaults
  object the service layer's `GET` path returns when no document exists yet
  (see "Seedless by design" above). A plain function, not a classmethod on
  `SiteSettingsRead`, purely so it reads clearly at the call site
  (`default_site_settings()`) — no behavioral difference either way.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

# Literal, hardcoded primary key of the one document this collection will
# ever contain — see this module's "Singleton mechanism" docstring section.
# A plain string (not an ObjectId/PyObjectId) is deliberate: this collection's
# "identity" is structural (there is exactly one of it), not an opaque
# reference to one of many rows.
SITE_SETTINGS_SINGLETON_ID: str = "site_settings"


def _reject_blank_or_control_chars(value: str, *, field_name: str) -> str:
    """Shared validator body for `SiteVerificationCode.name`/`.content` — see
    module docstring's "Field caps and why" section for the full reasoning.
    Not reused outside this file; kept as a plain function (not a Pydantic
    validator itself) so both fields can share the same check text without
    duplicating the raise message.
    """
    if not value.strip():
        raise ValueError(f"{field_name} must not be empty or whitespace-only")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{field_name} must not contain control characters (e.g. newlines/tabs)")
    return value


def _reject_blank_or_control_chars_only_when_nonempty(value: str, *, field_name: str) -> str:
    """Shared validator body for `head_injection_code`/`body_injection_code`
    — see module docstring's "Field caps and why" section for the full
    reasoning. Deliberately **not** a reuse of `_reject_blank_or_control_chars`
    above: these two fields are multi-line code (real `<script>` snippets have
    newlines/tabs throughout), so rejecting any control character would break
    every legitimate value. The empty string is always valid (the "not
    configured yet" default, per ADR-024) and is never rejected here; only a
    *non-empty* value that reduces to nothing once whitespace and non-printable
    control characters are stripped out (e.g. a stray pasted newline with no
    real code) is rejected, per ADR-024's "reject blank/control-character-only
    values" — read as "blank-or-control-character-*only*", not "empty" and not
    "contains any control character".
    """
    if value == "":
        return value
    printable = "".join(ch for ch in value if not (ch.isspace() or ord(ch) < 32 or ord(ch) == 127))
    if not printable:
        raise ValueError(f"{field_name} must not be blank or control-character-only")
    return value


class SiteVerificationCode(BaseModel):
    """One `{name, content}` search-engine verification entry.

    Fully provider-agnostic by design (founder correction, see module
    docstring): `name` holds the literal meta-tag name itself (e.g.
    `google-site-verification`, `msvalidate.01`, `yandex-verification`, or
    any future provider's own meta name), rendered generically through
    Next.js's `Metadata.verification.other` for every entry — no
    special-cased per-provider field on this model.
    """

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description=(
            "The literal meta-tag name, e.g. 'google-site-verification', "
            "'msvalidate.01', 'yandex-verification' — rendered verbatim via "
            "Next.js's Metadata.verification.other, no special-cased mapping."
        ),
    )
    content: str = Field(
        ...,
        min_length=1,
        max_length=200,
        description="The verification code value provided by the search engine/provider.",
    )

    @field_validator("name")
    @classmethod
    def _validate_name(cls, value: str) -> str:
        return _reject_blank_or_control_chars(value, field_name="name")

    @field_validator("content")
    @classmethod
    def _validate_content(cls, value: str) -> str:
        return _reject_blank_or_control_chars(value, field_name="content")


class SiteSettingsBase(BaseModel):
    """Round 1 fields (`ads_txt_content`, `verification_codes`) plus Round 2's
    `head_injection_code`/`body_injection_code` (ADR-024, now Approved and
    implemented) — see module docstring's "Round 2 (implemented)" section."""

    model_config = ConfigDict(extra="forbid")

    ads_txt_content: str = Field(
        default="",
        max_length=50000,
        description=(
            "Plain-text ads.txt file content, served verbatim as "
            "Content-Type: text/plain by the frontend's app/ads.txt/route.ts "
            "— not HTML-escaped/sanitized here, see module docstring's "
            "'Field caps and why' section for why that's the correct "
            "posture for this specific field."
        ),
    )
    verification_codes: list[SiteVerificationCode] = Field(
        default_factory=list,
        max_length=50,
        description=(
            "Provider-agnostic list of {name, content} search-engine "
            "verification entries — see SiteVerificationCode. Capped at 50, "
            "not the exact count of known providers — see module docstring."
        ),
    )
    head_injection_code: str = Field(
        default="",
        max_length=20000,
        description=(
            "Raw HTML/JS injected verbatim into every public page's <head> "
            "(e.g. a GTM/analytics snippet) — no sanitization pipeline runs "
            "on this value at any layer, per ADR-024's Decision. Empty "
            "string is the valid 'not configured yet' default; see module "
            "docstring's 'Field caps and why' section for the cap and "
            "validator reasoning."
        ),
    )
    body_injection_code: str = Field(
        default="",
        max_length=20000,
        description=(
            "Raw HTML/JS injected verbatim into every public page's <body> "
            "(e.g. a noscript pixel/chat-widget snippet) — no sanitization "
            "pipeline runs on this value at any layer, per ADR-024's "
            "Decision. Empty string is the valid 'not configured yet' "
            "default; see module docstring's 'Field caps and why' section "
            "for the cap and validator reasoning."
        ),
    )

    @field_validator("verification_codes")
    @classmethod
    def _reject_duplicate_names(
        cls, value: list["SiteVerificationCode"]
    ) -> list["SiteVerificationCode"]:
        """`code-reviewer` flagged (Low, non-blocking): two entries with the
        same `name` (e.g. an admin accidentally pasting the same provider
        twice) both pass without this check, but `app/layout.tsx`'s
        `Object.fromEntries(verificationCodes.map(...))` silently collapses
        them to whichever comes last when building `Metadata.verification.
        other` - a plausible copy-paste mistake that would otherwise drop
        data with no error anywhere in the chain. Exact-string match (not
        case-insensitive): meta-tag names are conventionally lowercase and an
        exact duplicate is what actually collides in `Object.fromEntries`."""
        seen: set[str] = set()
        for code in value:
            if code.name in seen:
                raise ValueError(f"duplicate verification code name: {code.name!r}")
            seen.add(code.name)
        return value

    @field_validator("head_injection_code")
    @classmethod
    def _validate_head_injection_code(cls, value: str) -> str:
        return _reject_blank_or_control_chars_only_when_nonempty(value, field_name="head_injection_code")

    @field_validator("body_injection_code")
    @classmethod
    def _validate_body_injection_code(cls, value: str) -> str:
        return _reject_blank_or_control_chars_only_when_nonempty(value, field_name="body_injection_code")


class SiteSettingsUpdate(SiteSettingsBase):
    """Request body for `PUT /v1/content/site-settings` (admin,
    `require_admin`). Full-replace PUT, not a partial update.
    `head_injection_code`/`body_injection_code` are overridden here as
    REQUIRED (no default) even though `SiteSettingsBase` defaults them to
    "": a PUT that omits them would otherwise silently overwrite stored
    injected code with "" (code-reviewer finding). The inherited validator
    and 20000 cap still apply. `ads_txt_content`/`verification_codes` keep
    their Base defaults (Round 1 behavior, unchanged). See module docstring's
    "Shape notes" section."""

    head_injection_code: str = Field(
        ...,
        max_length=20000,
        description=(
            "Required on PUT (send \"\" to clear) - see SiteSettingsBase."
            "head_injection_code."
        ),
    )
    body_injection_code: str = Field(
        ...,
        max_length=20000,
        description=(
            "Required on PUT (send \"\" to clear) - see SiteSettingsBase."
            "body_injection_code."
        ),
    )


class SiteSettingsDocument(SiteSettingsBase):
    """Shape of the one `site_settings` document as read back from MongoDB.
    Only ever instantiated from an actual `find_one` result — see module
    docstring's "Seedless by design" section for the no-document-yet case,
    which returns `SiteSettingsRead` via `default_site_settings()` instead of
    this class."""

    model_config = ConfigDict(populate_by_name=True)

    id: str = Field(
        default=SITE_SETTINGS_SINGLETON_ID,
        alias="_id",
        description=(
            "Always SITE_SETTINGS_SINGLETON_ID — a plain string, not a "
            "PyObjectId, unlike every sibling collection's _id. See module "
            "docstring's 'Singleton mechanism' section."
        ),
    )
    created_at: datetime
    updated_at: datetime


class SiteSettingsRead(SiteSettingsBase):
    """API response shape for both `GET /v1/content/site-settings` (public)
    and `PUT /v1/content/site-settings` (admin) — just the `SiteSettingsBase`
    fields (Round 1 + Round 2), no document metadata exposed. See module
    docstring's "Shape notes" section for why `id`/`created_at`/`updated_at`
    are deliberately absent here even though `SiteSettingsDocument` carries
    them."""


def default_site_settings() -> SiteSettingsRead:
    """The sane, all-empty defaults `GET /v1/content/site-settings` returns
    when no document has ever been written yet (spec acceptance criterion 3
    — "returns sane empty defaults before any admin write ever happens").

    Deliberately a plain function, not a DB call and not a classmethod on
    `SiteSettingsRead` — see module docstring's "Shape notes" section. Never
    inserts anything into Mongo; the service layer's GET path must stay
    side-effect-free (see module docstring's "Seedless by design" section).
    """

    return SiteSettingsRead(
        ads_txt_content="",
        verification_codes=[],
        head_injection_code="",
        body_injection_code="",
    )
