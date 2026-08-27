"""Content Idea Generator service, backed by OpenRouter (ADR-018).

Tier 2 (Processing, via Job System) per the approved feature-spec
(docs/roadmap/SPRINT_STATUS.md, 2026-08-27 entry "feature-spec approved:
Content Idea Generator via OpenRouter") - same latency-justification as
`keyword_research.py`'s docstring: an LLM generation call, materially
higher/more variable latency than Grammar Checker's single bounded external
call, so this runs async via job submit -> poll `GET /jobs/{id}`.

`generate_content_ideas()` is the callable the Job System's Processor layer
(`app/services/ai/processors.py`'s `ContentIdeaGeneratorProcessor`) is
expected to call during `execute()`.

Filed under `app/services/ai/` alongside `keyword_research.py`,
`grammar_checker.py`, and `openrouter_client.py`, matching the Handbook
Part I.2/ADR-015 module placement for AI-heavy tools, and mirroring
`keyword_research.py`'s structure/docstring conventions closely - this is
v1-parity port of `pdfconverterai.com/tools/seo/content-idea-generator`
(confirmed live pre-spec), the third `ai_tools` OpenRouter pilot.

Unlike Keyword Research's flat JSON *array* output, this tool's output is a
JSON *object* with exactly three named fields (`blogTitles`,
`videoConcepts`, `socialPosts`) - a different parse target, flagged as a
risk in the approved feature-spec. Live-verified (3 direct OpenRouter API
calls against the real `OPENROUTER_MODEL`, different topics, during this
task's implementation) that the fixed model reliably returns a
directly-parseable JSON object with all three keys populated each time -
no code-fence wrapping or surrounding prose observed in any of the 3
samples (5.18s/8.09s/8.51s, all comfortably under
`openrouter_client.DEFAULT_TIMEOUT_SECONDS`). The same defensive
code-fence-stripping / regex-fallback approach as `_extract_json_array` is
still kept as a safety net below, since free-tier model behavior isn't
guaranteed stable over time (see `openrouter_client.py`'s own model-churn
warning) - not because the 3-sample check found it necessary.
"""
import json
import logging
import re

from app.services.ai.openrouter_client import OpenRouterUnavailableError, call_openrouter

logger = logging.getLogger(__name__)

# Same bound as `keyword_research.py`'s MAX_SEED_KEYWORD_LENGTH - a short
# phrase, not a document.
MAX_TOPIC_LENGTH = 200

# v1-parity output shape: three named categories, per the approved
# feature-spec.
_IDEA_FIELDS = ("blogTitles", "videoConcepts", "socialPosts")

# Same fence-stripping convention as `keyword_research.py`.
_CODE_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE | re.MULTILINE)

# Fallback: the first `{...}`-shaped span in the response, greedy across
# newlines, for when the model pads its reply with prose around the object -
# mirrors `keyword_research.py`'s `_JSON_ARRAY_RE`, adapted for an object
# rather than an array.
_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.DOTALL)


class ContentIdeaGeneratorUnavailableError(Exception):
    """Raised when content idea generation can't be completed - either
    OpenRouter itself is unavailable (`OpenRouterUnavailableError` from
    `openrouter_client.py`, including its shared circuit breaker being
    tripped) or the model's response couldn't be parsed into the expected
    structured JSON object shape (mitigates the approved feature-spec's
    named risk: a 3-key JSON object is a different parse target than
    Keyword Research's flat array - degrade to unavailable rather than
    return garbage or a partial/missing category).

    Callers only need to catch this single exception type (not
    `OpenRouterUnavailableError` separately - it's wrapped/re-raised as
    this below).

    Expectation for `ContentIdeaGeneratorProcessor`
    (`app/services/ai/processors.py`): catch this exception during
    `execute()` and call
    `mark_failed(job_id, "Content idea generation is temporarily "
    "unavailable, please try again shortly")` (or equivalent clean,
    generic message) - the same translation `KeywordResearchProcessor`
    applies for `KeywordResearchUnavailableError`. Never leak the
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
        "You are a content marketing idea generator. Given a topic, generate "
        "content ideas useful for a content calendar.\n\n"
        f"Topic: {topic}\n\n"
        "Respond with ONLY a JSON object (no prose, no markdown code fences, "
        "no explanation before or after it) with exactly these three fields, "
        "each an array of exactly 5 short strings:\n"
        '  - "blogTitles": array of 5 blog post title ideas\n'
        '  - "videoConcepts": array of 5 short video/content concepts\n'
        '  - "socialPosts": array of 5 short social media post ideas\n\n'
        "Example response shape:\n"
        '{"blogTitles": ["Example Title"], "videoConcepts": '
        '["Example concept"], "socialPosts": ["Example post"]}'
    )


def _extract_json_object(raw_response: str) -> dict:
    """Defensively extracts a JSON object from a model response that may be
    wrapped in a markdown code fence and/or padded with prose, before
    handing it to `json.loads` - same rationale as
    `keyword_research.py`'s `_extract_json_array`, adapted for an object
    (three named fields) rather than a flat array.

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


async def generate_content_ideas(topic: str) -> dict:
    """Generates AI content ideas (blog titles, video concepts, social
    posts) for `topic` via a single OpenRouter chat-completion call.

    Args:
        topic: the user-supplied content topic/keyword.

    Returns:
        {"topic": <str>, "blogTitles": [<str>, ...],
        "videoConcepts": [<str>, ...], "socialPosts": [<str>, ...]}
        Ideas are LLM-generated, not sourced from real trend/SEO data -
        callers/UI must label this clearly as an AI-generated suggestion,
        same convention as Keyword Research's estimated volume/competition.

    Raises:
        ValueError: `topic` is empty/blank or over `MAX_TOPIC_LENGTH`
            characters - callers map this to a 400 (or, inside a job, a
            permanent/non-retried failure).
        ContentIdeaGeneratorUnavailableError: OpenRouter (including the
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
        logger.error("🚫 OpenRouter unavailable for content idea generation: %s", str(e))
        raise ContentIdeaGeneratorUnavailableError(
            "OpenRouter is temporarily unavailable"
        ) from e

    try:
        parsed = _extract_json_object(raw_response)
        ideas = _normalize_ideas(parsed)
    except (ValueError, TypeError) as e:
        logger.error(
            "💥 Failed to parse OpenRouter content idea generation response: %s", str(e)
        )
        raise ContentIdeaGeneratorUnavailableError(
            "Content idea generation response could not be parsed"
        ) from e

    logger.debug(
        "Content idea generation returned %d blog title(s), %d video concept(s), "
        "%d social post(s)",
        len(ideas["blogTitles"]),
        len(ideas["videoConcepts"]),
        len(ideas["socialPosts"]),
    )
    return {"topic": topic, **ideas}
