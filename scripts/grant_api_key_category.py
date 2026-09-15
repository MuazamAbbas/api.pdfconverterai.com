#!/usr/bin/env python3
"""Grant an existing `api_keys` document an additional `categories` entry.

Exists because the "new backend category/router ships, but the
`frontend-service` API key's `categories` array is never granted it" class of
bug has already shipped broken to production twice (Merge PDF 2026-08-04,
Text-to-Binary 2026-08-13) from doing this ad-hoc over SSH/`mongosh`, and was
flagged again during the 2026-09-05 Tools Metadata CMS cleanup: an inline
`mongosh` one-liner typed directly against production is an obfuscation risk
with no diff, no review, and no re-run history. This script is the
reviewable, re-runnable replacement for that one-liner - a real file in the
repo, not something typed in the moment. See CLAUDE.md's "New backend
category/router -> check the `frontend-service` API key grant" rule and
Handbook Part D.1.

Deliberately general-purpose, not analytics- (or `frontend-service`-)
specific: `--owner` and `--category` are both plain arguments, because this
exact class of grant recurs almost every time a new router ships (SPRINT_STATUS.md
has this done by hand for `ai_tools`, `seo_tools`, `calculators`, `web_tools`,
`image`, and more - each its own ad-hoc Mongo write) and is worth having one
audited tool for instead of a new bespoke command each time.

Safety properties:
    - Reuses `app.core.database`'s `db` (same connection convention as
      `seed_content_categories.py`/`seed_admin.py`), so it always picks up
      `DATABASE_URL` from whatever environment/.env it's run against - never
      a hardcoded connection string, never a value read or printed from a
      .env file directly (see `read_env_field.py`'s docstring for why that
      matters here).
    - The key's `key` field (the actual secret credential) is NEVER read,
      printed, or logged, at any point, in any line - identification is by
      `owner` and `categories` only, matching the precedent already used in
      this project (SPRINT_STATUS.md's 2026-08-21 entry: "reusing the app's
      own DB module, printing only owner/categories, never the key value").
    - Applies the grant via `$addToSet` ONLY - never `$set`/overwrite of the
      whole `categories` array. A full-array overwrite is the exact root
      cause shape of both prior production-breaking incidents named above;
      `$addToSet` can only ever add the one requested category, never drop
      an existing one.
    - Idempotent and safely re-runnable: if the category is already present,
      this is a clean no-op ("already granted") - no error, no duplicate
      entry, no second write.
    - Prints the CURRENT `categories` array before the update and the
      RESULTING `categories` array after, so the operator gets an explicit
      before/after diff in the same run - never trust "it probably worked",
      verify it in the output.

Usage (run from the `backend/` directory so `app.*` imports resolve):
    python scripts/grant_api_key_category.py --owner frontend-service --category analytics

Requires `DATABASE_URL` to already be set in the environment/.env this
script is run against, same requirement `app/core/config.py` has to boot at
all.

This mutates a production authorization document. Per CLAUDE.md/the AI
Collaboration rules, get explicit founder approval before running this
against the production database - writing/reviewing the script is not the
same as approval to run it.
"""
import argparse
import asyncio
import os
import sys

# Allow running as `python scripts/grant_api_key_category.py` from the
# `backend/` directory without needing `backend/` pre-added to PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--owner",
        required=True,
        help="The api_keys document's `owner` field value, e.g. frontend-service",
    )
    parser.add_argument(
        "--category",
        required=True,
        help="The category string to add to `categories`, e.g. analytics",
    )
    return parser.parse_args()


async def _run() -> int:
    args = _parse_args()
    owner = args.owner
    category = args.category

    # Imported after arg parsing - app.core.config.Settings() is evaluated
    # at import time and raises loudly (via pydantic) if DATABASE_URL is
    # missing, which is the correct fail-closed behavior.
    from app.core.database import db

    key_doc = await db.api_keys.find_one({"owner": owner})
    if key_doc is None:
        # Fail loudly rather than silently no-op-ing - a typo'd --owner here
        # is exactly the kind of mistake that would otherwise look like a
        # successful grant while touching nothing at all.
        print(f"ERROR: no api_keys document found with owner={owner!r}", file=sys.stderr)
        return 1

    current_categories = key_doc.get("categories", [])
    print(f"owner={owner!r} current categories={current_categories!r}")

    if category in current_categories:
        print(f"already granted: {category!r} is already in owner={owner!r}'s categories - no change made")
        return 0

    # $addToSet only - never $set/overwrite the whole array. This is the
    # exact root-cause shape of both prior production-breaking incidents
    # (Merge PDF 2026-08-04, Text-to-Binary 2026-08-13) this script exists
    # to prevent a recurrence of.
    update_result = await db.api_keys.update_one(
        {"owner": owner},
        {"$addToSet": {"categories": category}},
    )
    if update_result.modified_count != 1:
        print(
            f"ERROR: update matched but did not modify owner={owner!r}'s document "
            f"(matched_count={update_result.matched_count}, modified_count={update_result.modified_count})",
            file=sys.stderr,
        )
        return 1

    updated_doc = await db.api_keys.find_one({"owner": owner})
    new_categories = updated_doc.get("categories", []) if updated_doc else None
    print(f"GRANTED: owner={owner!r} category={category!r}")
    print(f"owner={owner!r} resulting categories={new_categories!r}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
