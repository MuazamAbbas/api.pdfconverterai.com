#!/usr/bin/env python3
"""One-time (idempotent, re-runnable) script to seed the `homepage_sections`
collection (the `admin` module, ADR-019 - Homepage Sections CMS) with the
three sections currently hardcoded in `frontend/app/page.tsx`:

  1. `hero`     - order 0, the `<h1>`/`<p>` copy
  2. `ad_slot`  - order 1, the reserved AdSense zone (`placement_id`,
                  `height_px` matching the current `h-24` = 96px)
  3. `tool_grid`- order 2, empty `content: {}` (registry-driven, never
                  admin-authored - see app/schemas/homepage_section.py)

Mirrors `scripts/seed_admin.py`'s precedent: CLI/env-driven, run from the
`backend/` directory so `app.*` imports resolve, safe to re-run without
duplicating documents.

Idempotency: unlike `admin_users` (unique on `email`) or `tool_grid`
(the DB-level partial unique index on `type`), `hero`/`ad_slot` have no
uniqueness constraint at the DB layer - by design, a future iteration may
want more than one banner/ad_slot section. This script therefore does its
own "does a document of this `type` already exist" check per section
before inserting, so re-running it never creates duplicates of the three
sections it seeds.

The hero subheading is a static equivalent of `frontend/app/page.tsx`'s
`{totalTools}+ free, instant tools for everyday work: ...` paragraph
(dropping the live `{totalTools}` interpolation, which lives in a
TypeScript registry file this Python script has no reliable way to parse)
rather than a hardcoded tool count that would silently drift from the
registry over time.

Usage (run from the `backend/` directory so `app.*` imports resolve):
    python scripts/seed_homepage_sections.py

Requires `DATABASE_URL` to already be set in the environment/.env this
script is run against (same requirement `app/core/config.py` has to boot
at all).
"""
import asyncio
import os
import sys

# Allow running as `python scripts/seed_homepage_sections.py` from the
# `backend/` directory without needing `backend/` pre-added to PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _run() -> int:
    # Imported after sys.path is set up - app.core.config.Settings() is
    # evaluated at import time and raises loudly (via pydantic) if
    # DATABASE_URL is missing, which is the correct fail-closed behavior.
    from app.core.database import db
    from app.schemas.homepage_section import HomepageSectionCreate, SectionType

    sections = [
        HomepageSectionCreate(
            type=SectionType.HERO,
            order=0,
            enabled=True,
            content={
                "heading": "PDF, image, text, SEO, and network tools — all in one place.",
                "subheading": (
                    "Free, instant tools for everyday work: merge and split PDFs, "
                    "compress images, check SEO and DNS, convert units, generate "
                    "secure passwords, and more. No signup, no clutter."
                ),
            },
        ),
        HomepageSectionCreate(
            type=SectionType.AD_SLOT,
            order=1,
            enabled=True,
            content={"placement_id": "homepage-primary", "height_px": 96},
        ),
        HomepageSectionCreate(
            type=SectionType.TOOL_GRID,
            order=2,
            enabled=True,
            content={},
        ),
    ]

    created = 0
    skipped = 0
    for section in sections:
        existing = await db.homepage_sections.find_one({"type": section.type.value})
        if existing is not None:
            print(f"SKIP: a '{section.type.value}' section already exists (id={existing['_id']})")
            skipped += 1
            continue
        insert_result = await db.homepage_sections.insert_one(section.model_dump())
        print(f"CREATED: '{section.type.value}' section id={insert_result.inserted_id} order={section.order}")
        created += 1

    print(f"Done. created={created} skipped={skipped}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
