"""Pydantic models for the `tags` collection.

New collection for the shared content-taxonomy foundation (the new
`content` module, ADR-021 — founder-approved architecture decisions),
flagged per CLAUDE.md's "don't invent a new collection without flagging it"
rule, same convention `content_categories` (this feature) and
`admin_users`/`homepage_sections` before it were flagged under.

Backs free-form tagging for the two queued CMS systems (Tools Metadata CMS,
Blog/News CMS — neither is built by this task). Per ADR-021's "Option C:
normalize-and-upsert" decision, **there is no direct create/update/delete
API for this collection, and there never should be** — the only write path
into `tags` is `app/services/content/tags_service.py::get_or_create_tag`,
which normalizes an incoming raw string (trim, lowercase, slugify) and
upserts it. A future backend-builder must not add a direct CRUD route for
`tags` — if either CMS needs to attach tags to a content item, it calls
`get_or_create_tag` per raw tag string and stores the returned canonical
`slug`, never a raw admin-typed string directly. Only a read-only
list/autocomplete route (backend-builder, not part of this task) is
expected on top of this collection.

Collection: `tags` (lowercase-plural). Fields: snake_case, matching
`content_categories`'/`homepage_sections`'/`admin_users`' convention within
this same collection family (see `content_category.py`'s docstring for why
this diverges from Handbook Part C.9's camelCase default).

**Indexing decisions** (see `app/core/database.py::ensure_indexes` for the
actual index creation):

- `slug` (unique, ascending): the actual normalize-and-upsert guarantee.
  `get_or_create_tag`'s own find-before-upsert check is a
  TOCTOU-vulnerable first line of defense only (same caveat
  `admin_user_service.create_admin_user`'s docstring gives for its own
  find-before-insert check) - this index is what actually prevents two
  concurrent callers normalizing "SEO"/"seo" at the same moment from ever
  creating two `seo` documents.
- No `order` index: unlike `content_categories`/`homepage_sections`, tags
  have no admin-curated display order - the future autocomplete/list route
  is expected to sort alphabetically by `slug` (already indexed) or by
  usage frequency computed elsewhere, not a stored `order` field.
- No TTL index: tags are not transient processing/upload metadata (Handbook
  C.9's TTL rule doesn't apply) - once created, a tag persists even if no
  content item currently references it (no reference-counting/garbage
  collection is implemented by this foundation task).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.schemas.common import PyObjectId


class TagDocument(BaseModel):
    """Shape of a `tags` document as read back from MongoDB. There is no
    corresponding `TagCreate`/`TagUpdate` shape - see this module's
    docstring: the only writer is `get_or_create_tag`, which builds the
    insert document inline rather than through a caller-supplied Pydantic
    create model (there's nothing for a caller to legitimately set beyond
    the raw tag string itself)."""

    model_config = ConfigDict(populate_by_name=True, arbitrary_types_allowed=True)

    id: PyObjectId = Field(alias="_id")
    slug: str = Field(..., min_length=1, max_length=100)
    label: str = Field(..., min_length=1, max_length=100, description="First-seen display casing")
    created_at: datetime
