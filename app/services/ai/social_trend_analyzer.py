"""Social Trend Analyzer service, backed by OpenRouter (ADR-018).

Tier 2 (Processing, via Job System) per the approved feature-spec
(docs/roadmap/SPRINT_STATUS.md, 2026-08-29 entry "feature-spec approved:
Social Trend Analyzer via OpenRouter") - same latency-justification as
`content_idea_generator.py`'s docstring: an LLM generation call, materially
higher/more variable latency than Grammar Checker's single bounded external
call, so this runs async via job submit -> poll `GET /jobs/{id}`.

`generate_social_trends()` is the callable the Job System's Processor layer
(`app/services/ai/processors.py`'s `SocialTrendAnalyzerProcessor`) is
expected to call during `execute()`.

Filed under `app/services/ai/` alongside `keyword_research.py`,
`content_idea_generator.py`, `grammar_checker.py`, and `openrouter_client.py`,
matching the Handbook Part I.2/ADR-015 module placement for AI-heavy tools,
and mirroring `content_idea_generator.py`'s structure/docstring conventions
closely - this is a v1-parity port of
`pdfconverterai.com/tools/seo/social-trend-analyzer` (confirmed live
pre-spec), the fourth `ai_tools` OpenRouter pilot.

Unlike Content Idea Generator's 3-key JSON object output, this tool's output
is a JSON *object* with exactly two named fields (`hashtags`,
`contentIdeas`) - a different parse target than either Content Idea
Generator's 3-key shape or Keyword Research's flat array, flagged as a risk
in the approved feature-spec (2-key parse reliability is NOT assumed to
transfer from the 3-key Content Idea Generator result). Live-verified (3
direct OpenRouter API calls against the real `OPENROUTER_MODEL`, different
topics, during this task's implementation) that the fixed model reliably
returns a directly-parseable JSON object with both keys populated each time
- no code-fence wrapping or surrounding prose observed in any of the 3
samples (topics "electric bikes", "home coffee brewing", "budget travel
photography"; 2.33s/2.63s/4.48s, all comfortably under
`openrouter_client.DEFAULT_TIMEOUT_SECONDS`). The third call
("budget travel photography") hit one transient 429 from OpenRouter's
upstream shared free-tier pool for this model first (confirmed via the raw
response body: `"limit_source":"upstream_provider_shared_pool"`, a
provider-side capacity limit, not our own account's daily quota) and
succeeded on retry ~30s later - consistent with `call_openrouter`'s existing
429 handling (`OpenRouterUnavailableError`, circuit-breaker `_record_failure`)
and not itself a parse-reliability concern. The same defensive
code-fence-stripping / regex-fallback approach as
`content_idea_generator.py`'s `_extract_json_object` is still kept as a
safety net below, since free-tier model behavior isn't guaranteed stable
over time (see `openrouter_client.py`'s own model-churn warning) - not
because the 3-sample check found it necessary.
"""
import json
import logging
import re

from app.services.ai.openrouter_client import OpenRouterUnavailableError, call_openrouter

logger = logging.getLogger(__name__)

# Same bound as `content_idea_generator.py`'s/`keyword_research.py`'s
# MAX_TOPIC_LENGTH/MAX_SEED_KEYWORD_LENGTH - a short phrase, not a document.
MAX_TOPIC_LENGTH = 200

# v1-parity output shape: two named categories, per the approved
# feature-spec.
_IDEA_FIELDS = ("hashtags", "contentIdeas")

# Same fence-stripping convention as `content_idea_generator.py`/
# `keyword_research.py`.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

# Fallback: the first `{...}`-shaped span in the response, greedy across
# newlines, for when the model pads its reply with prose around the object -
# mirrors `content_idea_generator.py`'s `_JSON_OBJECT_RE`.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class SocialTrendAnalyzerUnavailableError(Exception):
    """Raised when social trend analysis can't be completed - either
    OpenRouter itself is unavailable (`OpenRouterUnavailableError` from
    `openrouter_client.py`, including its shared circuit breaker being
    tripped) or the model's response couldn't be parsed into the expected
    structured JSON object shape (mitigates the approved feature-spec's
    named risk: a 2-key JSON object is a different parse target than both
    Content Idea Generator's 3-key object and Keyword Research's flat array
    - degrade to unavailable rather than return garbage or a
    partial/missing category).

    Callers only need to catch this single exception type (not
    `OpenRouterUnavailableError` separately - it's wrapped/re-raised as
    this below).

    Expectation for `SocialTrendAnalyzerProcessor`
    (`app/services/ai/processors.py`): catch this exception during
    `execute()` and call
    `mark_failed(job_id, "Social trend analysis is temporarily "
    "unavailable, please try again shortly")` (or equivalent clean,
    generic message) - the same translation `ContentIdeaGeneratorProcessor`
    applies for `ContentIdeaGeneratorUnavailableError`. Never leak the
    underlying OpenRouter/library/parsing exception text (Handbook
    Part C.10).
    """


def _validate_topic(topic: str) -> str:
    if topic is None or not topic.strip():
        raise ValueError("Topic cannot be empty")
    topic = topic.strip()
    if len(topic) > MAX_TOPIC_LENGTH:
        raise ValueError(f"Topic must be at most {MAX_TOPIC_LENGTH} characters")
    return topic


def _build_prompt(topic: str) -> str:
    return (
        "You are a social media trend analyst. Given a topic or keyword, "
        "suggest trending hashtags and content angle ideas useful for "
        "social media posts (Instagram, TikTok, Twitter/X, Facebook).\n\n"
        f"Topic: {topic}\n\n"
        "Respond with ONLY a JSON object (no prose, no markdown code fences, "
        "no explanation before or after it) with exactly these two fields:\n"
        '  - "hashtags": array of up to 5 short hashtag strings, each '
        'including the leading "#"\n'
        '  - "contentIdeas": array of up to 5 short content idea strings, '
        "each framed for a social platform (Instagram/TikTok/Twitter/"
        "Facebook)\n\n"
        "Example response shape:\n"
        '{"hashtags": ["#Example"], "contentIdeas": '
        '["Post a short Reel showing an example use case"]}'
    )


def _extract_json_object(raw_response: str) -> dict:
    """Defensively extracts a JSON object from a model response that may be
    wrapped in a markdown code fence and/or padded with prose, before
    handing it to `json.loads` - same rationale as
    `content_idea_generator.py`'s `_extract_json_object`, adapted for this
    tool's 2-key shape.

    Raises ValueError if no JSON object could be located/parsed.
    """
    text = (raw_response or "").strip()
    stripped = _CODE_FENCE_RE.sub("", text).strip()

    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except (json.JSONDecodeError, TypeError):
        pass

    match = _JSON_OBJECT_RE.search(stripped)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except (json.JSONDecodeError, TypeError):
            pass

    raise ValueError("Could not locate a valid JSON object in the model response")


def _normalize_idea_list(items) -> list:
    if not isinstance(items, list):
        raise ValueError("Expected a list of idea strings")
    normalized = [item.strip() for item in items if isinstance(item, str) and item.strip()]
    if not normalized:
        raise ValueError("Idea list contained no usable strings")
    return normalized


def _normalize_ideas(parsed: dict) -> dict:
    missing = [field for field in _IDEA_FIELDS if field not in parsed]
    if missing:
        raise ValueError(f"Model response is missing required field(s): {', '.join(missing)}")

    return {field: _normalize_idea_list(parsed[field]) for field in _IDEA_FIELDS}


async def generate_social_trends(topic: str) -> dict:
    """Generates AI trending hashtags and social content ideas for `topic`
    via a single OpenRouter chat-completion call.

    Args:
        topic: the user-supplied content topic/keyword.

    Returns:
        {"topic": <str>, "hashtags": [<str>, ...], "contentIdeas": [<str>, ...]}
        Ideas are LLM-generated, not sourced from real trend/social data -
        callers/UI must label this clearly as an AI-generated suggestion,
        same convention as Keyword Research's estimated volume/competition
        and Content Idea Generator's ideas.

    Raises:
        ValueError: `topic` is empty/blank or over `MAX_TOPIC_LENGTH`
            characters - callers map this to a 400 (or, inside a job, a
            permanent/non-retried failure).
        SocialTrendAnalyzerUnavailableError: OpenRouter (including the
            shared circuit breaker) was unavailable, or the model's
            response couldn't be parsed into the expected structured JSON
            object shape - see the class docstring for how a Processor
            should translate this into a job failure.
    """
    topic = _validate_topic(topic)
    prompt = _build_prompt(topic)

    try:
        raw_response = await call_openrouter(prompt)
    except OpenRouterUnavailableError as e:
        logger.error("🚫 OpenRouter unavailable for social trend analysis: %s", str(e))
        raise SocialTrendAnalyzerUnavailableError(
            "OpenRouter is temporarily unavailable"
        ) from e

    try:
        parsed = _extract_json_object(raw_response)
        ideas = _normalize_ideas(parsed)
    except (ValueError, TypeError) as e:
        logger.error(
            "💥 Failed to parse OpenRouter social trend analysis response: %s", str(e)
        )
        raise SocialTrendAnalyzerUnavailableError(
            "Social trend analysis response could not be parsed"
        ) from e

    logger.debug(
        "Social trend analysis returned %d hashtag(s), %d content idea(s)",
        len(ideas["hashtags"]),
        len(ideas["contentIdeas"]),
    )
    return {"topic": topic, **ideas}
