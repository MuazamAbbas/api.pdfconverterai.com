"""Shared OpenRouter LLM client for the `ai_tools` module (ADR-018).

Built OpenRouter-wide, not scoped to any single tool - Keyword Research
(`app/services/ai/keyword_research.py`) is the first caller, but ADR-018
names three more planned OpenRouter-backed pilots (Social Trend Analyzer,
SEO Audit, Content Idea Generator) that should reuse this same client and
circuit breaker rather than each reimplementing their own.

Filed under `app/services/ai/` alongside `grammar_checker.py`, matching
the Handbook Part I.2/ADR-015 module placement for AI-heavy tools. Follows
`grammar_checker.py`'s timeout/error-handling shape closely (`aiohttp`,
`aiohttp.ClientTimeout`, catch `aiohttp.ClientError`/`asyncio.TimeoutError`,
raise a custom "unavailable" exception on timeout/429/5xx/unexpected
status, never leak raw exception text to callers per Handbook Part C.10).
"""
import asyncio
import logging
import time
from typing import Optional

import aiohttp

from app.core.config import settings

logger = logging.getLogger(__name__)

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

# Fixed free-tier model (ADR-018 decision: a single fixed model, no
# per-request picker). All three candidates named in the approved
# feature-spec (docs/roadmap/SPRINT_STATUS.md, 2026-08-27 entry) -
# `meta-llama/llama-3.1-8b-instruct:free`,
# `mistralai/mistral-small-3.1-24b-instruct:free`, and
# `google/gemma-3-27b-it:free` - turned out to be retired from OpenRouter's
# free tier by the time of live verification (2026-08-27): each now
# returns a 404 with `"This model is unavailable for free..."`, confirmed
# via a direct curl against the OpenRouter API, not assumed from our own
# client's error handling. `GET /api/v1/models` was queried live to find
# what's actually free today.
#
# First replacement pick, `nvidia/nemotron-3-super-120b-a12b:free`, was
# live-verified once at ~3.5s but then hit a real 15s+ timeout in a
# same-session end-to-end job-pipeline re-test - a reasoning model with
# widely variable latency is a bad fit for `DEFAULT_TIMEOUT_SECONDS`.
# Settled on `liquid/lfm-2.5-2.6b:free` instead: a small, non-reasoning
# model, live-verified 3 times in a row (4.97s/7.78s/10.44s - consistently
# well under the 15s timeout) returning a directly-parseable JSON array
# (no code-fence/prose wrapping needed) matching the exact
# `keyword_research.py` prompt schema each time. Revisit if OpenRouter
# retires this one too, or if a later OpenRouter pilot needs a materially
# different quality/latency profile (ADR-018 trade-offs) - re-verify live
# via `GET /api/v1/models` rather than trusting this list to still be
# accurate; OpenRouter's free-tier lineup churns, and per-model latency is
# not reliably predictable from a single sample - test at least 2-3 calls
# with the real prompt shape before trusting a pick.
OPENROUTER_MODEL = "liquid/lfm-2.5-2.6b:free"

# 25s, not 15s: the observed free-tier latency samples for the current
# OPENROUTER_MODEL trended upward across 3 live runs (4.97s/7.78s/10.44s -
# see that constant's comment), and the previous model choice's failure
# mode was exactly a timeout at the old 15s ceiling. `app/worker.py`'s
# `job_timeout = 300` for ai_keyword_research leaves ample room for a
# bigger per-call ceiling here without risking the overall job timeout.
DEFAULT_TIMEOUT_SECONDS = 25

# --- Circuit breaker ---------------------------------------------------
#
# Module-level, in-memory state - NOT Mongo/Redis-backed. This is only
# globally correct because `call_openrouter` is exclusively invoked from
# `KeywordResearchProcessor.execute()` (app/services/ai/processors.py),
# which only ever runs inside the single-process `arq-worker.service`
# (`arq app.worker.WorkerSettings` - see app/worker.py), NOT inside
# gunicorn. `gunicorn.conf.py` runs 2 worker processes (`workers = 2`,
# confirmed by reading that file directly, not assumed) - if this client
# were ever called from a router/gunicorn-process context (e.g. a future
# Tier 1 OpenRouter-backed tool, for which Grammar Checker is precedent
# among ai_tools endpoints), the breaker state would silently split across
# gunicorn's 2 independent processes, each tripping/resetting on its own
# and defeating the "3-strike/5-min, OpenRouter-wide" acceptance criterion.
# Keep every OpenRouter-backed tool as a Tier 2 job routed through the ARQ
# worker (matching ADR-018's own Tier placement), or move this breaker to a
# Mongo/Redis-backed shared counter before adding a Tier 1 caller.
_CONSECUTIVE_FAILURE_THRESHOLD = 3
_COOLDOWN_SECONDS = 5 * 60

_consecutive_failures = 0
_breaker_tripped_until: Optional[float] = None
_breaker_lock = asyncio.Lock()


class OpenRouterUnavailableError(Exception):
    """Raised when OpenRouter can't service a request: the circuit breaker
    is currently tripped, or the request timed out, failed to connect, was
    rate-limited (429), errored (5xx), or returned an unexpected
    status/response shape.

    Callers must degrade this to a clean, generic "temporarily unavailable"
    response/failure - never leak the underlying aiohttp/library exception
    detail (Handbook Part C.10). See
    `keyword_research.py`'s `KeywordResearchUnavailableError` for how a
    tool-specific service wraps this for its own callers, and that
    exception's docstring for how the eventual Processor (added in a
    separate worker-wiring task) should translate a caught error into a
    job failure.
    """


def _breaker_is_tripped_locked() -> bool:
    """Must be called while holding `_breaker_lock`."""
    return _breaker_tripped_until is not None and time.monotonic() < _breaker_tripped_until


async def _record_failure() -> None:
    global _consecutive_failures, _breaker_tripped_until
    async with _breaker_lock:
        _consecutive_failures += 1
        if _consecutive_failures >= _CONSECUTIVE_FAILURE_THRESHOLD:
            _breaker_tripped_until = time.monotonic() + _COOLDOWN_SECONDS
            logger.error(
                "🚫 OpenRouter circuit breaker tripped after %d consecutive failure(s) - "
                "cooling down for %d seconds",
                _consecutive_failures,
                _COOLDOWN_SECONDS,
            )


async def _record_success() -> None:
    global _consecutive_failures
    async with _breaker_lock:
        _consecutive_failures = 0


async def is_breaker_tripped() -> bool:
    """Nice-to-have external check (e.g. an early-reject at request time
    before even attempting a call). Not required for the primary 503/error
    behavior - `call_openrouter` itself short-circuits without making the
    HTTP call whenever the breaker is tripped, which is the main "AI tools
    temporarily unavailable" mechanism."""
    async with _breaker_lock:
        return _breaker_is_tripped_locked()


async def call_openrouter(prompt: str, *, timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS) -> str:
    """POSTs `prompt` as a single user message to OpenRouter's chat-completions
    endpoint using the fixed `OPENROUTER_MODEL`, and returns the assistant's
    reply text content.

    Raises:
        OpenRouterUnavailableError: the circuit breaker is currently open
            (short-circuits before any HTTP call is made), or the request
            timed out, failed to connect, was rate-limited (429), errored
            (5xx/unexpected status), or the response didn't have the
            expected `choices[0].message.content` shape.
    """
    async with _breaker_lock:
        if _breaker_is_tripped_locked():
            logger.warning("🚫 OpenRouter circuit breaker is open - short-circuiting call")
            raise OpenRouterUnavailableError("OpenRouter circuit breaker is open")

    timeout = aiohttp.ClientTimeout(total=timeout_seconds)
    headers = {
        "Authorization": f"Bearer {settings.openrouter_api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": OPENROUTER_MODEL,
        "messages": [{"role": "user", "content": prompt}],
    }

    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.post(OPENROUTER_URL, headers=headers, json=payload) as response:
                if response.status == 429:
                    logger.warning("🚫 OpenRouter API rate limit hit")
                    await _record_failure()
                    raise OpenRouterUnavailableError("Rate limited by OpenRouter API")
                if response.status >= 500:
                    logger.error("💥 OpenRouter API server error: %s", response.status)
                    await _record_failure()
                    raise OpenRouterUnavailableError("OpenRouter API server error")
                if response.status != 200:
                    logger.error("💥 OpenRouter API unexpected status: %s", response.status)
                    await _record_failure()
                    raise OpenRouterUnavailableError(
                        f"OpenRouter API returned status {response.status}"
                    )
                data = await response.json()
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        logger.exception("💥 Error calling OpenRouter API: %s", str(e))
        await _record_failure()
        raise OpenRouterUnavailableError("Could not reach the OpenRouter API") from e

    try:
        content = data["choices"][0]["message"]["content"]
        if not isinstance(content, str):
            raise TypeError("content is not a string")
    except (KeyError, IndexError, TypeError) as e:
        logger.error("💥 Unexpected OpenRouter API response shape: %s", str(e))
        await _record_failure()
        raise OpenRouterUnavailableError("Unexpected OpenRouter API response shape") from e

    await _record_success()
    return content
