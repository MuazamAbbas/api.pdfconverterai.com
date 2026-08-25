"""Grammar Checker service, backed by the public LanguageTool API.

Tier 1 (Instant) per Handbook Part I.2 and the approved feature-spec
(docs/roadmap/SPRINT_STATUS.md, 2026-08-25 entry "feature-spec approved:
Grammar Checker via LanguageTool") - a single bounded external HTTP call,
same shape as `web_tools/validate_url`. No job/queue involvement.

Calls `POST https://api.languagetool.org/v2/check` directly - no
self-hosted LanguageTool server, no new HTTP-client dependency (`aiohttp`
is already used elsewhere in this codebase, e.g.
`app/routers/web_tools.py`).

Filed under `app/services/ai/` (the ADR-015 target module name for
AI-heavy tools), matching the pattern the feature-spec asked for even
though the router this is wired into (`app/routers/ai_tools.py`) hasn't
yet been renamed/re-prefixed per ADR-015 Open Item 3 - that prefix
migration is separate, larger reconciliation work, out of scope here.
"""
import asyncio
import logging

import aiohttp

logger = logging.getLogger(__name__)

LANGUAGETOOL_URL = "https://api.languagetool.org/v2/check"

# The public (anonymous, unauthenticated) LanguageTool API tier's documented
# text-length limit.
MAX_TEXT_LENGTH = 20_000

DEFAULT_LANGUAGE = "en-US"

# Single bounded external call - never let a slow/hanging upstream stall
# this request indefinitely (acceptance criterion 3).
REQUEST_TIMEOUT_SECONDS = 10


class GrammarCheckerUnavailableError(Exception):
    """Raised when the upstream LanguageTool API can't service the request
    (timeout, connection error, HTTP 429, or HTTP 5xx). Callers must degrade
    this to a clean, generic "temporarily unavailable" response - never
    leak the underlying aiohttp/library exception detail (Handbook Part
    C.10)."""


def _validate_text(text: str) -> str:
    if text is None or not text.strip():
        raise ValueError("Text cannot be empty")
    if len(text) > MAX_TEXT_LENGTH:
        raise ValueError(f"Text must be at most {MAX_TEXT_LENGTH} characters")
    return text


def _map_issue(match: dict) -> dict:
    rule = match.get("rule") or {}
    category = rule.get("category") or {}
    replacements = match.get("replacements") or []
    return {
        "ruleId": rule.get("id"),
        "type": rule.get("issueType") or category.get("id"),
        "message": match.get("message"),
        "offset": match.get("offset"),
        "length": match.get("length"),
        "replacements": [
            r.get("value") for r in replacements if r.get("value") is not None
        ],
    }


def _apply_corrections(text: str, matches: list) -> str:
    """Deterministically builds a "corrected text" by applying the first
    replacement suggestion for each match. Matches are applied right-to-left
    by offset so rewriting one match never invalidates the offsets of
    matches still to be applied. A match with no replacement suggestions (or
    a malformed offset/length) is left untouched in the output rather than
    guessed at. If a match's span genuinely overlaps a later (further-right)
    match that was already applied, it is skipped rather than applied - two
    overlapping rewrites both touching `corrected` would corrupt offsets
    that were only ever validated against the original `text`."""
    applicable = [
        m
        for m in matches
        if (m.get("replacements") or [{}])[0].get("value") is not None
        and isinstance(m.get("offset"), int)
        and isinstance(m.get("length"), int)
        and m["offset"] >= 0
        and m["length"] >= 0
    ]
    applicable.sort(key=lambda m: m["offset"], reverse=True)

    corrected = text
    consumed_from = len(text)
    for match in applicable:
        offset = match["offset"]
        length = match["length"]
        if offset + length > consumed_from:
            continue
        replacement_value = match["replacements"][0]["value"]
        corrected = corrected[:offset] + replacement_value + corrected[offset + length:]
        consumed_from = offset
    return corrected


async def check_grammar(text: str, language: str = DEFAULT_LANGUAGE) -> dict:
    """Checks `text` against the public LanguageTool API and returns
    corrected text plus a structured issues list.

    Raises:
        ValueError: input is empty, blank, or over `MAX_TEXT_LENGTH` -
            callers map this to a 400.
        GrammarCheckerUnavailableError: the upstream API timed out, refused
            the connection, rate-limited (429), or errored (5xx/unexpected
            status) - callers map this to a 503.
    """
    text = _validate_text(text)
    language = language or DEFAULT_LANGUAGE

    timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_SECONDS)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(
                LANGUAGETOOL_URL,
                data={"text": text, "language": language},
            ) as response:
                if response.status == 429:
                    logger.warning("🚫 LanguageTool API rate limit hit")
                    raise GrammarCheckerUnavailableError("Rate limited by LanguageTool API")
                if response.status >= 500:
                    logger.error("💥 LanguageTool API server error: %s", response.status)
                    raise GrammarCheckerUnavailableError("LanguageTool API server error")
                if response.status != 200:
                    logger.error("💥 LanguageTool API unexpected status: %s", response.status)
                    raise GrammarCheckerUnavailableError(
                        f"LanguageTool API returned status {response.status}"
                    )
                payload = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.exception("💥 Error calling LanguageTool API: %s", str(e))
        raise GrammarCheckerUnavailableError("Could not reach the LanguageTool API") from e

    matches = payload.get("matches") or []
    issues = [_map_issue(m) for m in matches]
    corrected_text = _apply_corrections(text, matches)

    result = {
        "text": text,
        "language": language,
        "correctedText": corrected_text,
        "issues": issues,
    }
    logger.debug("Grammar check found %d issue(s)", len(issues))
    return result
