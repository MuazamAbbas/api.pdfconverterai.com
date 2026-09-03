"""Transactional email dispatch - the `notification` module (ADR-020,
approved). First code in this module: the codebase's first email-sending
capability.

Deliberately its own module rather than folded into `auth`: `auth` (the
`POST /auth/users/password-reset/request` route + `user_service.py`) mints
the token and enqueues a Tier 2 ARQ job, but never talks to Resend's API
itself - `app/worker.py`'s `send_password_reset_email` task calls
`send_email` here instead. Keeps "who is this" (auth) separate from "how do
we notify them" (notification), one module = one responsibility (Handbook
Part C.3) - the same reasoning ADR-020's Decision section gives for this
split.

Resend (ADR-020 Decision + Options table: third-party transactional email
API, not self-hosted SMTP). Uses `aiohttp` - the same async HTTP client
`app/services/ai/openrouter_client.py` already uses for ITS external API
call, which (like this one) is only ever invoked from the ARQ worker
process, not a gunicorn/FastAPI request process (see that module's
docstring for the concurrency reasoning). Deliberately not `httpx` (a
project dependency too, but only otherwise used by the test suite) or
`requests` (synchronous - would block the worker's event loop for the
duration of the HTTP call).

`send_email(to, template, context)`: template-based, not a raw subject/html
parameter pair, so no caller ever hand-builds email markup - only this
module knows what a given email actually contains. Round 1 only needs one
template (`password_reset`); add more here as future features need them,
never a second ad-hoc template mechanism elsewhere.
"""
import asyncio
import logging
from typing import Any, Callable

import aiohttp

from app.core.config import settings

logger = logging.getLogger(__name__)

RESEND_API_URL = "https://api.resend.com/emails"

# Same timeout/error-handling shape as
# app/services/ai/openrouter_client.py's call_openrouter (ADR-018
# precedent): a short bounded timeout, and the raw aiohttp/library
# exception is never allowed past this module (Handbook Part C.10) -
# callers only ever see EmailSendError with a generic message.
DEFAULT_TIMEOUT_SECONDS = 15


class EmailSendError(Exception):
    """Raised when Resend can't be reached or rejects the request. Callers
    (currently only `app/worker.py::send_password_reset_email`) must not
    let this crash their job silently - see that task's own retry handling.
    """


def _render_password_reset(context: dict[str, Any]) -> tuple[str, str, str]:
    """Returns (subject, html_body, text_body) for the `password_reset`
    template. `context["reset_link"]` is required - never logged anywhere
    in this module, since a bare reset link is itself a bearer credential
    (same posture as never logging the raw token in
    `app/services/auth/user_service.py`)."""
    reset_link = context["reset_link"]
    expires_minutes = settings.user_password_reset_token_expires_minutes
    subject = "Reset your PDFConverterAI password"
    html_body = (
        "<p>We received a request to reset your PDFConverterAI password.</p>"
        f'<p><a href="{reset_link}">Click here to reset your password</a></p>'
        f"<p>This link expires in {expires_minutes} minutes. "
        "If you didn't request this, you can safely ignore this email.</p>"
    )
    text_body = (
        "We received a request to reset your PDFConverterAI password.\n\n"
        f"Reset your password: {reset_link}\n\n"
        f"This link expires in {expires_minutes} minutes. "
        "If you didn't request this, you can safely ignore this email."
    )
    return subject, html_body, text_body


_TEMPLATES: dict[str, Callable[[dict[str, Any]], tuple[str, str, str]]] = {
    "password_reset": _render_password_reset,
}


async def send_email(to: str, template: str, context: dict[str, Any]) -> None:
    """Renders `template` with `context` and dispatches it via the Resend
    API.

    Raises:
        EmailSendError: unknown template, request timed out/failed to
            connect, or Resend returned a non-2xx status. Never raises the
            underlying aiohttp exception directly (Handbook Part C.10).

    Never logs `to` beyond its use as the API payload's recipient, and
    never logs anything from `context` (may carry a reset link/token) -
    see `_render_password_reset`.
    """
    render = _TEMPLATES.get(template)
    if render is None:
        logger.error("send_email: unknown template %r", template)
        raise EmailSendError(f"Unknown email template: {template}")

    subject, html_body, text_body = render(context)

    payload = {
        "from": settings.resend_from_email,
        "to": [to],
        "subject": subject,
        "html": html_body,
        "text": text_body,
    }
    headers = {
        "Authorization": f"Bearer {settings.resend_api_key}",
        "Content-Type": "application/json",
    }

    timeout = aiohttp.ClientTimeout(total=DEFAULT_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(RESEND_API_URL, headers=headers, json=payload) as response:
                if response.status >= 400:
                    body_text = await response.text()
                    # Never log `to` (or `body_text` verbatim) - Resend's
                    # payload-validation error responses can echo back
                    # request fields, including the recipient address, which
                    # would put a user's email into server logs on a merely
                    # failed send. Redact it out of the raw body before
                    # logging so failures stay debuggable without
                    # persisting the address itself.
                    redacted_body = body_text[:500].replace(to, "[REDACTED_EMAIL]")
                    logger.error(
                        "Resend API rejected send (template=%s, status=%s): %s",
                        template, response.status, redacted_body,
                    )
                    raise EmailSendError(f"Resend API returned status {response.status}")
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.exception("Error calling Resend API (template=%s): %s", template, str(e))
        raise EmailSendError("Could not reach the Resend API") from e

    logger.info("Email dispatched via Resend (template=%s)", template)
