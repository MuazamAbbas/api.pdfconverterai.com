"""Tests for `app.services.ai.social_trend_analyzer.generate_social_trends` -
the callable `SocialTrendAnalyzerProcessor` (`app/services/ai/processors.py`)
is expected to invoke.

`call_openrouter` (from `app.services.ai.openrouter_client`) is monkeypatched
directly rather than faking `aiohttp` again here - `tests/
test_openrouter_client.py` already covers that client's own HTTP/circuit-
breaker behavior in isolation; this file focuses on
`social_trend_analyzer.py`'s own responsibilities: topic validation, prompt
building, and defensive JSON-*object*-response parsing (a different 2-key
parse target than both `content_idea_generator.py`'s 3-key object and
`keyword_research.py`'s flat array - see that file's/this service's module
docstring for why).
"""
import pytest

import app.services.ai.social_trend_analyzer as social_trend_analyzer_service
from app.services.ai.openrouter_client import OpenRouterUnavailableError
from app.services.ai.social_trend_analyzer import (
    MAX_TOPIC_LENGTH,
    SocialTrendAnalyzerUnavailableError,
    generate_social_trends,
)

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _patch_call_openrouter(monkeypatch, return_value=None, side_effect=None):
    async def _fake_call_openrouter(prompt, *, timeout_seconds=15):
        if side_effect is not None:
            raise side_effect
        return return_value

    monkeypatch.setattr(
        social_trend_analyzer_service, "call_openrouter", _fake_call_openrouter
    )


_VALID_JSON_RESPONSE = (
    '{"hashtags": ["#TagA", "#TagB"], '
    '"contentIdeas": ["Idea A", "Idea B"]}'
)


# --- validation ----------------------------------------------------------


async def test_generate_social_trends_rejects_empty_topic():
    with pytest.raises(ValueError, match="cannot be empty"):
        await generate_social_trends("")


async def test_generate_social_trends_rejects_whitespace_only_topic():
    with pytest.raises(ValueError, match="cannot be empty"):
        await generate_social_trends("   \n\t  ")


async def test_generate_social_trends_rejects_topic_over_max_length():
    with pytest.raises(ValueError, match=str(MAX_TOPIC_LENGTH)):
        await generate_social_trends("a" * (MAX_TOPIC_LENGTH + 1))


async def test_generate_social_trends_accepts_topic_at_max_length(monkeypatch):
    _patch_call_openrouter(monkeypatch, return_value=_VALID_JSON_RESPONSE)
    result = await generate_social_trends("a" * MAX_TOPIC_LENGTH)
    assert result["topic"] == "a" * MAX_TOPIC_LENGTH


async def test_generate_social_trends_strips_surrounding_whitespace(monkeypatch):
    _patch_call_openrouter(monkeypatch, return_value=_VALID_JSON_RESPONSE)
    result = await generate_social_trends("  electric bikes  ")
    assert result["topic"] == "electric bikes"


# --- happy path / JSON parsing --------------------------------------------


async def test_generate_social_trends_returns_parsed_ideas(monkeypatch):
    _patch_call_openrouter(monkeypatch, return_value=_VALID_JSON_RESPONSE)
    result = await generate_social_trends("electric bikes")

    assert result["topic"] == "electric bikes"
    assert result["hashtags"] == ["#TagA", "#TagB"]
    assert result["contentIdeas"] == ["Idea A", "Idea B"]


async def test_generate_social_trends_strips_markdown_code_fence(monkeypatch):
    fenced = f"```json\n{_VALID_JSON_RESPONSE}\n```"
    _patch_call_openrouter(monkeypatch, return_value=fenced)
    result = await generate_social_trends("electric bikes")
    assert result["hashtags"] == ["#TagA", "#TagB"]


async def test_generate_social_trends_extracts_object_from_surrounding_prose(monkeypatch):
    padded = f"Sure, here are some social trend ideas:\n{_VALID_JSON_RESPONSE}\nHope that helps!"
    _patch_call_openrouter(monkeypatch, return_value=padded)
    result = await generate_social_trends("electric bikes")
    assert result["contentIdeas"] == ["Idea A", "Idea B"]


async def test_generate_social_trends_strips_and_drops_blank_items(monkeypatch):
    response = (
        '{"hashtags": ["  #Padded  ", "", "   "], '
        '"contentIdeas": ["Idea A"]}'
    )
    _patch_call_openrouter(monkeypatch, return_value=response)
    result = await generate_social_trends("x")
    assert result["hashtags"] == ["#Padded"]


async def test_generate_social_trends_drops_non_string_items(monkeypatch):
    response = (
        '{"hashtags": ["#Valid", 42, null], '
        '"contentIdeas": ["Idea A"]}'
    )
    _patch_call_openrouter(monkeypatch, return_value=response)
    result = await generate_social_trends("x")
    assert result["hashtags"] == ["#Valid"]


# --- parse failures -> SocialTrendAnalyzerUnavailableError, not garbage --


async def test_generate_social_trends_not_json_raises_unavailable_not_garbage(monkeypatch):
    _patch_call_openrouter(monkeypatch, return_value="I cannot help with that request.")
    with pytest.raises(SocialTrendAnalyzerUnavailableError):
        await generate_social_trends("electric bikes")


async def test_generate_social_trends_json_array_not_object_raises_unavailable(monkeypatch):
    """Confirms the object-parsing path doesn't silently accept an array
    (a plausible model mistake given Keyword Research's sibling prompt asks
    for an array instead)."""
    _patch_call_openrouter(monkeypatch, return_value='["#TagA", "#TagB"]')
    with pytest.raises(SocialTrendAnalyzerUnavailableError):
        await generate_social_trends("electric bikes")


async def test_generate_social_trends_missing_field_raises_unavailable(monkeypatch):
    response = '{"hashtags": ["#TagA"]}'
    _patch_call_openrouter(monkeypatch, return_value=response)
    with pytest.raises(SocialTrendAnalyzerUnavailableError):
        await generate_social_trends("electric bikes")


async def test_generate_social_trends_empty_field_raises_unavailable(monkeypatch):
    response = '{"hashtags": [], "contentIdeas": ["Idea A"]}'
    _patch_call_openrouter(monkeypatch, return_value=response)
    with pytest.raises(SocialTrendAnalyzerUnavailableError):
        await generate_social_trends("electric bikes")


async def test_generate_social_trends_non_list_field_raises_unavailable(monkeypatch):
    response = '{"hashtags": "#TagA", "contentIdeas": ["Idea A"]}'
    _patch_call_openrouter(monkeypatch, return_value=response)
    with pytest.raises(SocialTrendAnalyzerUnavailableError):
        await generate_social_trends("electric bikes")


# --- OpenRouter unavailable propagates as SocialTrendAnalyzerUnavailableError


async def test_generate_social_trends_wraps_openrouter_unavailable_error(monkeypatch):
    _patch_call_openrouter(
        monkeypatch, side_effect=OpenRouterUnavailableError("circuit breaker is open")
    )
    with pytest.raises(SocialTrendAnalyzerUnavailableError) as exc_info:
        await generate_social_trends("electric bikes")
    # No leaked internal detail (Handbook Part C.10).
    assert "circuit breaker is open" not in str(exc_info.value)
