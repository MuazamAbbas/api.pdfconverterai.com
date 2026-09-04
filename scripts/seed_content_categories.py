#!/usr/bin/env python3
"""One-time (idempotent, re-runnable) script to seed the `content_categories`
collection (the new `content` module, ADR-021 - Content Model Foundation)
with the 10 existing tool categories hardcoded in
`frontend/lib/tools-registry.ts`, as `content_type: "tool_metadata"` rows.

Mirrors `scripts/seed_homepage_sections.py`'s precedent: writes directly to
`db.content_categories` (bypassing `app/services/content/categories_service.py`
entirely - the service layer's `create_category` actively rejects
`content_type='tool_metadata'` creates, since only this script may ever
create those rows, per that module's docstring), CLI/env-driven in the
sense that it needs no arguments, run from the `backend/` directory so
`app.*` imports resolve, safe to re-run without duplicating documents.

**CRITICAL - manual sync only, no automatic sync (ADR-021's trade-offs
section).** The category list below is a hand-copied snapshot of
`frontend/lib/tools-registry.ts`'s `toolsRegistry` array (`label`/`color`
per category, in registry order) and `frontend/THEME.md`'s
`CategoryColorSlug` -> `--category-{slug}` token map, confirmed by direct
inspection of both files on 2026-09-04. There is NO code path that keeps
this list in sync with either source file going forward. **If
`tools-registry.ts`'s category list is ever added to, renamed, reordered,
or removed, this script's `_TOOL_CATEGORIES` list must be updated by hand
to match** - a stale seed here will silently desync from the frontend nav/
color tokens with no automatic detection (exactly the risk ADR-021 accepts
and flags; re-check this file whenever `tools-registry.ts`'s category list
changes).

For every `tool_metadata` row, `slug` and `color_token` are deliberately
set to the *same* value - the `CategoryColorSlug` string itself (e.g.
`"web-network"`). There is no separate "content slug" identity for tool
categories beyond the color-token slug they already have site-wide; reusing
it avoids inventing a second identifier for the same category.

Idempotency: like `admin_users` (unique on `email`) and `homepage_sections`'
`tool_grid` (a DB-level partial unique index on `type`), `content_categories`
has a DB-level unique index on `slug` (app/core/database.py). This script
also does its own "does a document with this slug already exist" check
before inserting, so re-running it never creates duplicates and never
raises a raw `DuplicateKeyError` in the common (non-concurrent) case.

Usage (run from the `backend/` directory so `app.*` imports resolve):
    python scripts/seed_content_categories.py

Requires `DATABASE_URL` to already be set in the environment/.env this
script is run against (same requirement `app/core/config.py` has to boot
at all).
"""
import asyncio
import os
import sys

# Allow running as `python scripts/seed_content_categories.py` from the
# `backend/` directory without needing `backend/` pre-added to PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Hand-copied from `frontend/lib/tools-registry.ts` (registry order == the
# `order` field below) and `frontend/THEME.md`'s CategoryColorSlug table.
# See this script's module docstring - update BY HAND if the registry
# changes; nothing here re-derives it automatically.
_TOOL_CATEGORIES: list[tuple[str, str]] = [
    ("PDF", "pdf"),
    ("Image", "image"),
    ("Unit Converter", "unit-converter"),
    ("Text", "text"),
    ("Miscellaneous", "miscellaneous"),
    ("Cyber Security", "cyber-security"),
    ("Downloaders", "downloaders"),
    ("SEO", "seo"),
    ("Web / Network", "web-network"),
    ("AI Tools", "ai-tools"),
]


async def _run() -> int:
    # Imported after sys.path is set up - app.core.config.Settings() is
    # evaluated at import time and raises loudly (via pydantic) if
    # DATABASE_URL is missing, which is the correct fail-closed behavior.
    from app.core.database import db
    from app.schemas.content_category import ContentCategoryCreate, ContentType

    created = 0
    skipped = 0
    for order, (label, slug) in enumerate(_TOOL_CATEGORIES):
        category = ContentCategoryCreate(
            label=label,
            slug=slug,
            content_type=ContentType.TOOL_METADATA,
            color_token=slug,
            order=order,
        )
        existing = await db.content_categories.find_one({"slug": category.slug})
        if existing is not None:
            print(f"SKIP: a category with slug={category.slug!r} already exists (id={existing['_id']})")
            skipped += 1
            continue
        insert_result = await db.content_categories.insert_one(category.model_dump())
        print(f"CREATED: slug={category.slug!r} label={category.label!r} id={insert_result.inserted_id} order={order}")
        created += 1

    print(f"Done. created={created} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
