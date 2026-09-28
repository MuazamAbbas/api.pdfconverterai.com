#!/usr/bin/env python3
"""One-time script to delete an existing `admin_users` account (the `auth`
module).

This is the ONLY way an `admin_users` document is ever removed - there is
no `DELETE` HTTP route for this collection, by the same design as
`seed_admin.py`/`reset_admin_password.py` (no public/self-service account
management for this collection at all). Intended use: cleaning up a
short-lived admin account created for a one-off task (e.g. via
`seed_admin.py`) once that task is done, so no extra standing credential
is left behind.

Usage (run from the `backend/` directory so `app.*` imports resolve):
    python scripts/delete_admin_user.py --email temp-admin@pdfconverterai.com

Always shows the target account's id and creation timestamp, then requires
typing the email a second time at an interactive confirmation prompt,
before anything is deleted - deliberately no `--yes`/non-interactive
bypass, unlike `seed_admin.py`/`reset_admin_password.py`. Those two are
either non-destructive (seed fails loudly on conflict) or recoverable
(a reset password can be reset again); this one is the first genuinely
irreversible script in this family, so it gets no shortcut around the
interactive check - always run this directly from an interactive
terminal.

Don't run this against the account that is currently the *only*
`admin_users` document - that would lock out all admin access until
`seed_admin.py` is rerun. This script does not check for that; confirm
another admin account exists (or that you're fine reseeding one) first.

Requires `DATABASE_URL` and `ADMIN_JWT_SECRET` to already be set in the
environment/.env this script is run against, same as `seed_admin.py`.
"""
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


async def _run() -> int:
    email = os.environ.get("ADMIN_DELETE_EMAIL")
    if not email:
        email = input("Admin email to delete: ").strip()
    if not email or "@" not in email:
        print("ERROR: a valid email is required", file=sys.stderr)
        return 2

    # Imported after basic input validation, mirroring seed_admin.py:
    # app.core.config.Settings() is evaluated at import time and raises
    # loudly (via pydantic) if DATABASE_URL/ADMIN_JWT_SECRET are missing,
    # which is the correct fail-closed behavior here too.
    from app.services.auth.admin_user_service import (
        delete_admin_user,
        get_admin_user_by_email,
    )

    existing = await get_admin_user_by_email(email)
    if existing is None:
        print(f"ERROR: No admin_users document exists for {email!r}", file=sys.stderr)
        return 1

    print(
        f"Found admin_users account: email={existing.email} id={existing.id} "
        f"created_at={existing.created_at.isoformat()}"
    )
    confirmation = input(
        f"This will PERMANENTLY delete this account. Type the email again "
        f"to confirm ({existing.email!r}): "
    ).strip()
    if confirmation != existing.email:
        print("ERROR: confirmation did not match - aborting, nothing deleted", file=sys.stderr)
        return 2

    operator = getpass.getuser()

    try:
        await delete_admin_user(email=email, operator=operator)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Deleted admin_users document for {existing.email} (was id={existing.id}).")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
