#!/usr/bin/env python3
"""One-time script to create an `admin_users` account (the `auth` module).

This is the ONLY way an `admin_users` document is ever created - there is
no `/auth/register` or `/admin/signup` HTTP route, by design (no public
signup, ever, per this feature's approved architecture decisions). Run
this once per trusted operator, then have them log in via
`POST /api/v1/auth/login`.

Usage (run from the `backend/` directory so `app.*` imports resolve):
    python scripts/seed_admin.py --email admin@pdfconverterai.com --password 'a-strong-password'

Or via env vars (handy for a one-shot non-interactive VPS run without the
password appearing in shell history / `ps`):
    ADMIN_SEED_EMAIL=admin@pdfconverterai.com ADMIN_SEED_PASSWORD='a-strong-password' \
        python scripts/seed_admin.py

CLI args take precedence over env vars if both are given. The password is
never printed, logged, or echoed back - only a success/failure line naming
the email (not a secret) is shown.

Requires `DATABASE_URL` and `ADMIN_JWT_SECRET` to already be set in the
environment/.env this script is run against (the same two settings
`app/core/config.py` needs to boot at all) - see this repo's `.env` on the
VPS; `ADMIN_JWT_SECRET` itself is never printed by this script.
"""
import argparse
import asyncio
import getpass
import os
import sys

# Allow running as `python scripts/seed_admin.py` from the `backend/`
# directory without needing `backend/` pre-added to PYTHONPATH.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MIN_PASSWORD_LENGTH = 12


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        default=os.environ.get("ADMIN_SEED_EMAIL"),
        help="Admin email (falls back to ADMIN_SEED_EMAIL env var)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("ADMIN_SEED_PASSWORD"),
        help=(
            "Admin password (falls back to ADMIN_SEED_PASSWORD env var). "
            "If neither is given, you'll be prompted (hidden input, "
            "getpass) instead - the safest option for interactive use."
        ),
    )
    return parser.parse_args()


async def _run() -> int:
    args = _parse_args()

    email = args.email
    if not email:
        email = input("Admin email: ").strip()

    password = args.password
    if not password:
        password = getpass.getpass("Admin password (hidden): ")

    if not email or "@" not in email:
        print("ERROR: a valid email is required", file=sys.stderr)
        return 2
    if not password or len(password) < _MIN_PASSWORD_LENGTH:
        print(f"ERROR: password must be at least {_MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return 2

    # Imported after arg validation, and after ensuring ADMIN_JWT_SECRET/
    # DATABASE_URL are set - app.core.config.Settings() is evaluated at
    # import time and will raise (loudly, via pydantic) if a required
    # setting is missing, which is the correct fail-closed behavior here
    # too (this script shouldn't run against a misconfigured environment).
    from app.services.auth.admin_user_service import create_admin_user
    from app.services.auth.password_service import hash_password

    password_hash = hash_password(password)
    del password  # never touched again past this point

    try:
        admin = await create_admin_user(email=email, password_hash=password_hash)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(f"Created admin_users document for {admin.email} (id={admin.id})")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
