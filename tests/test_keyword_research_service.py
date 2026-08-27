"""Tests for `app.services.ai.keyword_research.research_keywords` - the
callable the (separately built) Keyword Research Processor/worker task is
expected to invoke.

`call_openrouter` (from `app.services.ai.openrouter_client`) is monkeypatched
directly rather than faking `aiohttp` again here - `tests/
test_openrouter_client.py` already covers that client's own HTTP/circuit-
breaker behavior in isolation; this file focuses on `keyword_research.py`'s
own responsibilities: seed-keyword validation, prompt building, and
defensive JSON-response parsing.
"""
import pytest

import app.services.ai.keyword_research as keyword_research_service
from app.services.ai.keyword_research import (
    MAX_SEED_KEYWORD_LENGTH,
    KeywordResearchUnavailableError,
    research_keywords,
)
from app.services.ai.openrouter_client import OpenRouterUnavailableError

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _patch_call_openrouter(monkeypatch, return_value=None, side_effect=None):
    async def _fake_call_openrouter(prompt, *, timeout_seconds=15):
        if side_effect is not None:
            raise side_effect
        return return_value

    monkeypatch.setattr(keyword_research_service, "call_openrouter", _fake_call_openrouter)


_VALID_JSON_RESPONSE = (
    '[{"keyword": "running shoes for flat feet", "estimatedVolume": 2400, '
    '"competition": "medium"}, {"keyword": "best trail running shoes", '
    '"estimatedVolume": 1900, "competition": "high"}]'
)


# --- validation ----------------------------------------------------------


async def test_research_keywords_rejects_empty_seed():
    with pytest.raises(ValueError, match="cannot be empty"):
        await research_keywords("")


async def test_research_keywords_rejects_whitespace_only_seed():
    with pytest.raises(ValueError, match="cannot be empty"):
        await research_keywords("   \n\t  ")


async def test_research_keywords_rejects_seed_over_max_length():
    with pytest.raises(ValueError, match=str(MAX_SEED_KEYWORD_LENGTH)):
        await research_keywords("a" * (MAX_SEED_KEYWORD_LENGTH + 1))


async def test_research_keywords_accepts_seed_at_max_length(monkeypatch):
    _patch_call_openrouter(monkeypatch, return_value=_VALID_JSON_RESPONSE)
    result = await research_keywords("a" * MAX_SEED_KEYWORD_LENGTH)
    assert result["seedKeyword"] == "a" * MAX_SEED_KEYWORD_LENGTH


async def test_research_keywords_strips_surrounding_whitespace(monkeypatch):
    _patch_call_openrouter(monkeypatch, return_value=_VALID_JSON_RESPONSE)
    result = await research_keywords("  running shoes  ")
    assert result["seedKeyword"] == "running shoes"


# --- happy path / JSON parsing --------------------------------------------


async def test_research_keywords_returns_parsed_suggestions(monkeypatch):
    _patch_call_openrouter(monkeypatch, return_value=_VALID_JSON_RESPONSE)
    result = await research_keywords("running shoes")

    assert result["seedKeyword"] == "running shoes"
    assert result["suggestions"] == [
        {"keyword": "running shoes for flat feet", "estimatedVolume": 2400, "competition": "medium"},
        {"keyword": "best trail running shoes", "estimatedVolume": 1900, "competition": "high"},
    ]


async def test_research_keywords_strips_markdown_code_fence(monkeypatch):
    fenced = f"```json\n{_VALID_JSON_RESPONSE}\n```"
    _patch_call_openrouter(monkeypatch, return_value=fenced)
    result = await research_keywords("running shoes")
    assert len(result["suggestions"]) == 2


async def test_research_keywords_extracts_array_from_surrounding_prose(monkeypatch):
    padded = f"Sure, here are some keyword suggestions:\n{_VALID_JSON_RESPONSE}\nHope that helps!"
    _patch_call_openrouter(monkeypatch, return_value=padded)
    result = await research_keywords("running shoes")
    assert len(result["suggestions"]) == 2


async def test_research_keywords_normalizes_competition_case(monkeypatch):
    response = '[{"keyword": "x", "estimatedVolume": 10, "competition": "LOW"}]'
    _patch_call_openrouter(monkeypatch, return_value=response)
    result = await research_keywords("x")
    assert result["suggestions"][0]["competition"] == "low"


async def test_research_keywords_skips_non_dict_items_in_array(monkeypatch):
    response = (
        '[{"keyword": "valid one", "estimatedVolume": 10, "competition": "low"}, '
        '"a stray string", 42]'
    )
    _patch_call_openrouter(monkeypatch, return_value=response)
    result = await research_keywords("x")
    assert result["suggestions"] == [{"keyword": "valid one", "estimatedVolume": 10, "competition": "low"}]


# --- parse failures -> KeywordResearchUnavailableError, not garbage -------


async def test_research_keywords_not_json_raises_unavailable_not_garbage(monkeypatch):
    _patch_call_openrouter(monkeypatch, return_value="I cannot help with that request.")
    with pytest.raises(KeywordResearchUnavailableError):
        await research_keywords("running shoes")


async def test_research_keywords_empty_array_raises_unavailable(monkeypatch):
    _patch_call_openrouter(monkeypatch, return_value="[]")
    with pytest.raises(KeywordResearchUnavailableError):
        await research_keywords("running shoes")


async def test_research_keywords_missing_required_field_raises_unavailable(monkeypatch):
    response = '[{"keyword": "missing volume and competition"}]'
    _patch_call_openrouter(monkeypatch, return_value=response)
    with pytest.raises(KeywordResearchUnavailableError):
        await research_keywords("running shoes")


async def test_research_keywords_invalid_competition_value_raises_unavailable(monkeypatch):
    response = '[{"keyword": "x", "estimatedVolume": 10, "competition": "extreme"}]'
    _patch_call_openrouter(monkeypatch, return_value=response)
    with pytest.raises(KeywordResearchUnavailableError):
        await research_keywords("running shoes")


async def test_research_keywords_non_numeric_volume_raises_unavailable(monkeypatch):
    response = '[{"keyword": "x", "estimatedVolume": "a lot", "competition": "low"}]'
    _patch_call_openrouter(monkeypatch, return_value=response)
    with pytest.raises(KeywordResearchUnavailableError):
        await research_keywords("running shoes")


# --- OpenRouter unavailable propagates as KeywordResearchUnavailableError -


async def test_research_keywords_wraps_openrouter_unavailable_error(monkeypatch):
    _patch_call_openrouter(
        monkeypatch, side_effect=OpenRouterUnavailableError("circuit breaker is open")
    )
    with pytest.raises(KeywordResearchUnavailableError) as exc_info:
        await research_keywords("running shoes")
    # No leaked internal detail (Handbook Part C.10).
    assert "circuit breaker is open" not in str(exc_info.value)
