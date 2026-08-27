"""Keyword Research service, backed by OpenRouter (ADR-018).

Tier 2 (Processing, via Job System) per the approved feature-spec
(docs/roadmap/SPRINT_STATUS.md, 2026-08-27 entry "feature-spec approved:
Keyword Research via OpenRouter") - unlike Grammar Checker's single bounded
external call, this involves an LLM generation call with materially
higher/more variable latency, justifying async job handling (submit -> poll
`GET /jobs/{id}`, same pattern as `pdf.py`).

`research_keywords()` is the callable the Job System's Processor layer (a
separate worker-wiring task, out of scope here - see
`app/services/jobs/service.py`/`app/services/*/processors.py` for the
established `validate/prepare/execute/verify/cleanup` shape) is expected to
call during `execute()`.

Filed under `app/services/ai/` alongside `grammar_checker.py` and
`openrouter_client.py`, matching the Handbook Part I.2/ADR-015 module
placement for AI-heavy tools, and mirroring `grammar_checker.py`'s
docstring/structure conventions.
"""
import json
import logging
import re

from app.services.ai.openrouter_client import OpenRouterUnavailableError, call_openrouter

logger = logging.getLogger(__name__)

# A generous but bounded ceiling for a "seed keyword" input - this is a
# short phrase, not a document (contrast `grammar_checker.py`'s
# `MAX_TEXT_LENGTH` of 20,000 for arbitrary text).
MAX_SEED_KEYWORD_LENGTH = 200

_VALID_COMPETITION_LEVELS = {"low", "medium", "high"}

# Strips a leading/trailing markdown code fence (```json ... ``` or ``` ... ```)
# that free-tier models commonly wrap structured output in despite being
# asked not to.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

# Fallback: the first `[...]`-shaped span in the response, greedy across
# newlines, for when the model pads its reply with prose around the array.
_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class KeywordResearchUnavailableError(Exception):
    """Raised when keyword research can't be completed - either OpenRouter
    itself is unavailable (`OpenRouterUnavailableError` from
    `openrouter_client.py`, including its shared circuit breaker being
    tripped) or the model's response couldn't be parsed into the expected
    structured JSON shape (mitigates the approved feature-spec's named
    risk: "free-tier model may produce inconsistent/unparseable structured
    output" - degrade to unavailable rather than return garbage).

    Callers only need to catch this single exception type (not
    `OpenRouterUnavailableError` separately - it's wrapped/re-raised as
    this below).

    Expectation for the Processor that will call `research_keywords()`
    (added by a separate worker-wiring task, not this one): catch this
    exception during `execute()` and call
    `mark_failed(job_id, "Keyword research is temporarily unavailable, "
    "please try again shortly")` (or equivalent clean, generic message) -
    the same translation `app/routers/ai_tools.py`'s `grammar_checker`
    endpoint does for `GrammarCheckerUnavailableError`, just landing on a
    job failure instead of a direct HTTP 503 response, since this runs
    inside the async worker rather than a synchronous route handler. Never
    leak the underlying OpenRouter/library/parsing exception text (Handbook
    Part C.10).
    """


def _validate_seed_keyword(seed_keyword: str) -> str:
    if seed_keyword is None or not seed_keyword.strip():
        raise ValueError("Seed keyword cannot be empty")
    seed_keyword = seed_keyword.strip()
    if len(seed_keyword) > MAX_SEED_KEYWORD_LENGTH:
        raise ValueError(f"Seed keyword must be at most {MAX_SEED_KEYWORD_LENGTH} characters")
    return seed_keyword


def _build_prompt(seed_keyword: str) -> str:
    return (
        "You are a keyword research assistant. Given a seed keyword, generate "
        "a list of 10 to 15 closely related keyword suggestions useful for "
        "SEO/content planning.\n\n"
        f"Seed keyword: {seed_keyword}\n\n"
        "Respond with ONLY a JSON array (no prose, no markdown code fences, no "
        "explanation before or after it) of objects, each with exactly these "
        "fields:\n"
        '  - "keyword": string, the suggested related keyword\n'
        '  - "estimatedVolume": number, your best estimate of monthly search '
        "volume\n"
        '  - "competition": string, one of "low", "medium", or "high"\n\n'
        "Example response shape:\n"
        '[{"keyword": "example keyword", "estimatedVolume": 1200, '
        '"competition": "medium"}]'
    )


def _extract_json_array(raw_response: str) -> list:
    """Defensively extracts a JSON array from a model response that may be
    wrapped in a markdown code fence and/or padded with prose, before
    handing it to `json.loads` - free-tier models don't reliably follow
    "respond with only JSON" instructions.

    Raises ValueError if no JSON array could be located/parsed.
    """
    text = (raw_response or "").strip()
    stripped = _CODE_FENCE_RE.sub("", text).strip()

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, list):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    match = _JSON_ARRAY_RE.search(stripped)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    raise ValueError("Could not locate a valid JSON array in the model response")


def _normalize_suggestion(item: dict) -> dict:
    keyword = item.get("keyword")
    volume = item.get("estimatedVolume")
    competition = item.get("competition")

    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("Suggestion is missing a valid 'keyword'")
    if isinstance(volume, bool) or not isinstance(volume, (int, float)):
        raise ValueError("Suggestion is missing a valid 'estimatedVolume'")
    if not isinstance(competition, str) or competition.lower() not in _VALID_COMPETITION_LEVELS:
        raise ValueError("Suggestion is missing a valid 'competition'")

    return {
        "keyword": keyword.strip(),
        "estimatedVolume": volume,
        "competition": competition.lower(),
    }


async def research_keywords(seed_keyword: str) -> dict:
    """Generates AI-estimated related-keyword suggestions for `seed_keyword`
    via a single OpenRouter chat-completion call.

    Args:
        seed_keyword: the user-supplied seed keyword/phrase.

    Returns:
        {"seedKeyword": <str>, "suggestions": [{"keyword": <str>,
        "estimatedVolume": <number>, "competition": "low"|"medium"|"high"},
        ...]}
        Volume/competition are LLM-estimated, not sourced from a real
        search-volume API - callers/UI must label this clearly as an AI
        estimate (approved feature-spec, Output section).

    Raises:
        ValueError: `seed_keyword` is empty/blank or over
            `MAX_SEED_KEYWORD_LENGTH` characters - callers map this to a
            400 (or, inside a job, a permanent/non-retried failure).
        KeywordResearchUnavailableError: OpenRouter (including the shared
            circuit breaker) was unavailable, or the model's response
            couldn't be parsed into the expected structured JSON shape -
            see the class docstring for how a Processor should translate
            this into a job failure.
    """
    seed_keyword = _validate_seed_keyword(seed_keyword)
    prompt = _build_prompt(seed_keyword)

    try:
        raw_response = await call_openrouter(prompt)
    except OpenRouterUnavailableError as e:
        logger.error("🚫 OpenRouter unavailable for keyword research: %s", str(e))
        raise KeywordResearchUnavailableError(
            "OpenRouter is temporarily unavailable"
        ) from e

    try:
        parsed_items = _extract_json_array(raw_response)
        suggestions = [
            _normalize_suggestion(item) for item in parsed_items if isinstance(item, dict)
        ]
        if not suggestions:
            raise ValueError("Model response contained no usable keyword suggestions")
    except (ValueError, TypeError) as e:
        logger.error("💥 Failed to parse OpenRouter keyword research response: %s", str(e))
        raise KeywordResearchUnavailableError(
            "Keyword research response could not be parsed"
        ) from e

    logger.debug("Keyword research returned %d suggestion(s)", len(suggestions))
    return {"seedKeyword": seed_keyword, "suggestions": suggestions}
