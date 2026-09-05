#!/usr/bin/env python3
"""One-time script to reset an existing `admin_users` account's password (the
`auth` module).

This is the ONLY out-of-band recovery path for an admin who is locked out or
has forgotten their password - there is no `/auth/forgot-password` HTTP route
for admin accounts, by design (mirrors `seed_admin.py`'s "no public
signup/reset route, ever" architecture decision). Run this once per recovery
event, then have the operator log in via `POST /api/v1/auth/login` and
change the password again themselves if they want to pick their own.

This also clears `failed_login_attempts` / `locked_until`, so a prior
lockout can't silently defeat the new password.

Usage (run from the `backend/` directory so `app.*` imports resolve):
    python scripts/reset_admin_password.py --email admin@pdfconverterai.com

    # Or supply your own new password instead of letting the script
    # generate one:
    python scripts/reset_admin_password.py --email admin@pdfconverterai.com --password 'a-strong-password'

If `--password`/`ADMIN_RESET_PASSWORD` is omitted, you'll get a hidden
(non-echoing, `getpass`) prompt - leave it blank and press Enter to have a
cryptographically-random password generated and printed ONCE to stdout
instead. Either way, this script never logs a plaintext password, and never
touches the *existing* password's plaintext (only the new one, and only
long enough to hash it).

Run this directly from an interactive terminal - don't redirect stdout to a
file (e.g. `> out.log`), which would durably persist a generated plaintext
password to disk.

Requires `DATABASE_URL` and `ADMIN_JWT_SECRET` to already be set in the
environment/.env this script is run against, same as `seed_admin.py`.
"""
import argparse
import asyncio
import getpass
import os
import secrets
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_MIN_PASSWORD_LENGTH = 12
_GENERATED_PASSWORD_LENGTH = 20  # ~119 bits of entropy via secrets.token_urlsafe


def _parse_args():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--email",
        default=os.environ.get("ADMIN_RESET_EMAIL"),
        help="Admin email whose password to reset (falls back to ADMIN_RESET_EMAIL env var)",
    )
    parser.add_argument(
        "--password",
        default=os.environ.get("ADMIN_RESET_PASSWORD"),
        help=(
            "New admin password (falls back to ADMIN_RESET_PASSWORD env var). "
            "If neither is given, a random one is generated and printed once."
        ),
    )
    return parser.parse_args()


async def _run() -> int:
    args = _parse_args()

    email = args.email
    if not email:
        email = input("Admin email to reset: ").strip()
    if not email or "@" not in email:
        print("ERROR: a valid email is required", file=sys.stderr)
        return 2

    generated = False
    password = args.password
    if not password:
        # Hidden prompt (like seed_admin.py's) rather than forcing --password/
        # ADMIN_RESET_PASSWORD, which would otherwise be the only way to pick
        # a specific password - and both are visible to other users on a
        # shared box via shell history / `ps` / `/proc/<pid>/environ`. Blank
        # input here falls back to a generated password instead.
        prompted = getpass.getpass(
            "New admin password (leave blank to auto-generate one, hidden input): "
        )
        if prompted:
            password = prompted
        else:
            password = secrets.token_urlsafe(_GENERATED_PASSWORD_LENGTH)
            generated = True

    if len(password) < _MIN_PASSWORD_LENGTH:
        print(f"ERROR: password must be at least {_MIN_PASSWORD_LENGTH} characters", file=sys.stderr)
        return 2

    # Imported after arg/password validation, mirroring seed_admin.py: app.core.
    # config.Settings() is evaluated at import time and raises loudly (via
    # pydantic) if DATABASE_URL/ADMIN_JWT_SECRET are missing, which is the
    # correct fail-closed behavior here too.
    from app.services.auth.admin_user_service import reset_admin_password
    from app.services.auth.password_service import hash_password

    password_hash = hash_password(password)
    operator = getpass.getuser()

    try:
        admin = await reset_admin_password(
            email=email, password_hash=password_hash, operator=operator
        )
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        password = None  # never touched again past this point
        return 1

    print(f"Reset password for admin_users document {admin.email} (id={admin.id}); lockout cleared.")
    if generated:
        print(f"New password (shown once, save it now): {password}")
    password = None  # never touched again past this point
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(_run()))
