"""Request schemas for the `auth` module's HTTP surface
(`app/routers/auth.py`). No response schema for login: per spec, the JWT
is never present in the JSON response body, only in the `Set-Cookie`
header, so a successful login's `data` is deliberately minimal (just the
admin's email, for the calling UI to display) rather than mirroring the
token anywhere.

Public User Auth (ADR-020, approved) request schemas live in this same
file (`UserSignupRequest`/`UserLoginRequest`/`PasswordResetRequestRequest`/
`PasswordResetConfirmRequest`) - same module (`auth`), same HTTP surface
file, just a second identity system per ADR-020's Option A. `AdminLoginRequest`
above is untouched.
"""
import re

from pydantic import BaseModel, Field, field_validator

# Deliberately simple format check (not the `email-validator` package,
# which isn't in requirements.txt) - this endpoint isn't public signup, it's
# a login form for a small, pre-seeded set of trusted operators, so
# strict RFC 5322 validation isn't worth a new dependency here. Reused
# as-is (not tightened) for the Public User Auth schemas below: adding a
# stricter email-validation dependency is out of Round 1 scope (ADR-020
# doesn't call for it), and this regex is exactly as strict either way.
_EMAIL_RE = r"^[^@\s]+@[^@\s]+\.[^@\s]+$"


class AdminLoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _validate_email_shape(cls, value: str) -> str:
        if not re.match(_EMAIL_RE, value.strip()):
            raise ValueError("Invalid email format")
        return value.strip().lower()


class UserSignupRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    # Round 1's only password-strength rule is a minimum length (ADR-020
    # doesn't call for anything more - complexity rules/breach-list checks
    # are out of scope). bcrypt (password_service.py) also silently caps
    # usable entropy around 72 bytes, so this max just keeps input sane,
    # not a meaningful strength requirement of its own.
    password: str = Field(..., min_length=8, max_length=128)
    # Honeypot (ADR-020 Trade-offs: rate limit + honeypot only, no CAPTCHA
    # for Round 1). The real signup form renders this field hidden
    # (off-screen/`display:none`) and never lets a human see or fill it -
    # only an automated bot filling every form field it finds would
    # populate it. Must stay empty; POST /auth/signup silently returns the
    # same success response without creating an account when it's non-empty
    # (never reveal to the caller that it was rejected as a bot).
    website: str = Field(default="", max_length=200)

    @field_validator("email")
    @classmethod
    def _validate_email_shape(cls, value: str) -> str:
        if not re.match(_EMAIL_RE, value.strip()):
            raise ValueError("Invalid email format")
        return value.strip().lower()


class UserLoginRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)
    password: str = Field(..., min_length=1, max_length=256)

    @field_validator("email")
    @classmethod
    def _validate_email_shape(cls, value: str) -> str:
        if not re.match(_EMAIL_RE, value.strip()):
            raise ValueError("Invalid email format")
        return value.strip().lower()


class PasswordResetRequestRequest(BaseModel):
    email: str = Field(..., min_length=3, max_length=254)

    @field_validator("email")
    @classmethod
    def _validate_email_shape(cls, value: str) -> str:
        if not re.match(_EMAIL_RE, value.strip()):
            raise ValueError("Invalid email format")
        return value.strip().lower()


class PasswordResetConfirmRequest(BaseModel):
    # secrets.token_urlsafe(32) (app/services/auth/user_service.py) produces
    # a 43-character token; generous bounds here are just a cheap
    # first-line sanity check ahead of the real hash-lookup validation, same
    # role SplitRequest.ranges' max_length plays in app/routers/pdf.py.
    token: str = Field(..., min_length=10, max_length=512)
    new_password: str = Field(..., min_length=8, max_length=128)
