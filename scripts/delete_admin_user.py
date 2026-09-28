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

Requires typing the account's email a second time at an interactive
confirmation prompt (skippable with `--yes` for scripted/non-interactive
use) before anything is deleted - this is destructive and irreversible,
unlike `seed_admin.py`/`reset_admin_password.py`.

Requires `DATABASE_URL` and `ADMIN_JWT_SECRET` to already be set in the
environment/.env this script is run against, same as `seed_admin.py`.
"""
import argparse
import asyncio
import getpass
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        default=os.environ.get("ADMIN_DELETE_EMAIL"),
        help="Admin email to delete (falls back to ADMIN_DELETE_EMAIL env var)",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help=(
            "Skip the interactive confirmation prompt - only for scripted/"
            "non-interactive use where the caller has already confirmed."
        ),
    )
    return parser.parse_args()


async def _run() -> int:
    args = _parse_args()

    email = args.email
    if not email:
        email = input("Admin email to delete: ").strip()
    if not email or "@" not in email:
        print("ERROR: a valid email is required", file=sys.stderr)
        return 2

    if not args.yes:
        confirmation = input(
            f"This will permanently delete the admin_users account for "
            f"{email!r}. Type the email again to confirm: "
        ).strip()
        if confirmation != email:
            print("ERROR: confirmation did not match - aborting, nothing deleted", file=sys.stderr)
            return 2

    # Imported after arg/confirmation checks, mirroring seed_admin.py: app.core.
    # config.Settings() is evaluated at import time and raises loudly (via
    # pydantic) if DATABASE_URL/ADMIN_JWT_SECRET are missing, which is the
    # correct fail-closed behavior here too.
    from app.services.auth.admin_user_service import delete_admin_user

    operator = getpass.getuser()

    try:
        await delete_admin_user(email=email, operator=operator)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Deleted admin_users document for {email}.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
