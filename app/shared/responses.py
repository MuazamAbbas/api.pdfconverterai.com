"""Standard API response envelope (Handbook Part C.5, ADR-004).

Every response is `{success, message, data}` on success or
`{success: false, message, error: {code}}` on failure. This is the one
place that shape is built so every router (new or being touched) produces
it the same way instead of hand-rolling dicts per route.

Pairs with the global exception handlers in `app/main.py`, which translate
any raised `HTTPException` (including the ones `api_error()` builds here)
into this same envelope at the top level of the JSON response.
"""
from typing import Any, Optional

from fastapi import HTTPException


def envelope(success: bool, message: str, data: Optional[Any] = None, error_code: Optional[str] = None) -> dict:
    if success:
        return {"success": True, "message": message, "data": data}
    return {"success": False, "message": message, "error": {"code": error_code}}


def api_error(status_code: int, message: str, error_code: str) -> HTTPException:
    """Build an HTTPException whose `detail` is already a full error envelope.

    Callers should `raise api_error(...)`. Never pass raw exception text or
    stack traces here - only safe, generic messages (Handbook Part C.10).
    """
    return HTTPException(status_code=status_code, detail=envelope(False, message, error_code=error_code))
