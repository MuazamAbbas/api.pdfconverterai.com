"""Request schemas for the `auth` module's HTTP surface
(`app/routers/auth.py`). No response schema for login: per spec, the JWT
is never present in the JSON response body, only in the `Set-Cookie`
header, so a successful login's `data` is deliberately minimal (just the
admin's email, for the calling UI to display) rather than mirroring the
token anywhere.
"""
import re

from pydantic import BaseModel, Field, field_validator

# Deliberately simple format check (not the `email-validator` package,
# which isn't in requirements.txt) - this endpoint isn't public signup, it's
# a login form for a small, pre-seeded set of trusted operators, so
# strict RFC 5322 validation isn't worth a new dependency here.
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
