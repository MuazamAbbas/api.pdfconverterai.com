"""Coverage for the 6 new Tier 1 instant SEO tool endpoints added on
`feature/seo-tools-batch1` (`app/routers/seo_tools.py`):

  - POST /v1/seo_tools/title_length_checker
  - POST /v1/seo_tools/meta_tag_generator
  - POST /v1/seo_tools/url_slug_generator
  - POST /v1/seo_tools/content_readability_analyzer
  - POST /v1/seo_tools/social_media_tags_generator
  - POST /v1/seo_tools/serp_preview

Follows the same conventions as `tests/test_seo_tools_robustness.py`:
Pydantic-layer failures (missing field, empty string violating
`min_length=1`, over `max_length`) are asserted as 422 (the shared
`RequestValidationError` handler in `conftest.build_test_app`);
service-layer `ValueError`s (whitespace-only input that satisfies
`min_length=1` but fails the service's own `.strip()` check, or other
domain-specific rejections like "no sluggable characters") are asserted as
400 (each router endpoint's own `except ValueError` branch). The
forced-500 test mirrors `test_seo_tools_robustness.py`'s technique:
monkeypatch the directly-imported service function reference on the
router module itself.

The `meta_tag_generator` and `social_media_tags_generator` XSS tests are
the most important tests in this file: both build an HTML snippet by
string-interpolating raw user input, so every field must be run through
`html.escape()` before being reflected back - see the `SECURITY:` comments
in `app/services/seo/meta_tag_generator.py` and
`app/services/seo/social_media_tags_generator.py`.
"""
import html

import pytest

import app.routers.seo_tools as seo_tools_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"

_SCRIPT_PAYLOAD = "<script>alert(1)</script>"
_BREAKOUT_PAYLOAD = '"><img src=x onerror=alert(1)>'


async def _boom(*args, **kwargs):
    raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/seo_tools.py")


# ---------------------------------------------------------------------------
# /title_length_checker
# ---------------------------------------------------------------------------

async def test_title_length_checker_valid_request_returns_200_with_correct_shape(client, api_key):
    title = "A Well-Sized Title For SEO Testing"  # 35 chars -> "good"
    resp = await client.post(
        "/v1/seo_tools/title_length_checker",
        json={"title": title},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == title
    assert body["length"] == len(title)
    assert body["word_count"] == len(title.split())
    assert body["status"] == "good"


async def test_title_length_checker_too_short_status(client, api_key):
    title = "Short title"  # 11 chars
    resp = await client.post(
        "/v1/seo_tools/title_length_checker",
        json={"title": title},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "too_short"


async def test_title_length_checker_too_long_status(client, api_key):
    title = "This Title Is Deliberately Written To Exceed The Sixty Character SERP Guidance Threshold"
    assert len(title) > 60
    resp = await client.post(
        "/v1/seo_tools/title_length_checker",
        json={"title": title},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "too_long"


async def test_title_length_checker_missing_field_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/title_length_checker",
        json={},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_title_length_checker_empty_string_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/title_length_checker",
        json={"title": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_title_length_checker_whitespace_only_returns_400(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/title_length_checker",
        json={"title": "     "},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_title_length_checker_over_max_length_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/title_length_checker",
        json={"title": "a" * 501},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_title_length_checker_very_long_input_returns_422_not_500(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/title_length_checker",
        json={"title": "a" * 10_050},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_title_length_checker_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(seo_tools_router, "check_title_length", _boom)
    resp = await client.post(
        "/v1/seo_tools/title_length_checker",
        json={"title": "A reasonably normal title"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to check title length"
    assert body["error"]["code"] == "TITLE_LENGTH_CHECK_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "seo_tools.py" not in resp.text


# ---------------------------------------------------------------------------
# /meta_tag_generator
# ---------------------------------------------------------------------------

async def test_meta_tag_generator_valid_request_returns_200_with_correct_shape(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={
            "title": "My Page Title",
            "description": "My page description.",
            "keywords": "pdf, converter, tools",
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "My Page Title"
    assert body["description"] == "My page description."
    assert body["keywords"] == "pdf, converter, tools"
    assert "<title>My Page Title</title>" in body["html_snippet"]
    assert 'name="description" content="My page description."' in body["html_snippet"]
    assert 'name="keywords" content="pdf, converter, tools"' in body["html_snippet"]


async def test_meta_tag_generator_keywords_optional_omitted_from_snippet(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={"title": "My Page Title", "description": "My page description."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["keywords"] is None
    assert "keywords" not in body["html_snippet"]


async def test_meta_tag_generator_missing_description_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={"title": "My Page Title"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_meta_tag_generator_empty_title_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={"title": "", "description": "desc"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_meta_tag_generator_whitespace_only_description_returns_400(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={"title": "My Page Title", "description": "    "},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_meta_tag_generator_title_over_max_length_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={"title": "a" * 501, "description": "desc"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_meta_tag_generator_description_very_long_returns_422_not_500(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={"title": "My Page Title", "description": "a" * 10_050},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_meta_tag_generator_keywords_over_max_length_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={"title": "My Page Title", "description": "desc", "keywords": "a" * 1001},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_meta_tag_generator_xss_payloads_are_html_escaped_in_snippet(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={
            "title": _SCRIPT_PAYLOAD,
            "description": _BREAKOUT_PAYLOAD,
            "keywords": _SCRIPT_PAYLOAD + _BREAKOUT_PAYLOAD,
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    snippet = body["html_snippet"]

    # The raw, unescaped attack strings must never appear anywhere in the
    # generated HTML snippet.
    assert "<script>" not in snippet
    assert "</script>" not in snippet
    assert '"><img' not in snippet

    # The HTML-escaped forms must be present instead (proves the payload
    # was reflected, just safely).
    assert html.escape(_SCRIPT_PAYLOAD) in snippet
    assert html.escape(_BREAKOUT_PAYLOAD) in snippet
    assert "&lt;script&gt;" in snippet
    assert "&quot;&gt;" in snippet

    # Note: the raw echoed `title`/`description`/`keywords` fields in the
    # response body are expected to contain the unescaped payload verbatim
    # (they are plain data fields, not HTML) - only `html_snippet` is an
    # HTML-context field and must be escaped. Asserting against the whole
    # `resp.text` would therefore be a false positive; the `snippet`-scoped
    # assertions above are the actual security check.


async def test_meta_tag_generator_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(seo_tools_router, "generate_meta_tags", _boom)
    resp = await client.post(
        "/v1/seo_tools/meta_tag_generator",
        json={"title": "My Page Title", "description": "desc"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to generate meta tags"
    assert body["error"]["code"] == "META_TAG_GENERATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text


# ---------------------------------------------------------------------------
# /url_slug_generator
# ---------------------------------------------------------------------------

async def test_url_slug_generator_valid_request_returns_200_with_correct_shape(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/url_slug_generator",
        json={"text": "Hello World! This Is A Café Test"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["original"] == "Hello World! This Is A Café Test"
    assert body["slug"] == "hello-world-this-is-a-cafe-test"


async def test_url_slug_generator_missing_field_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/url_slug_generator",
        json={},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_url_slug_generator_empty_string_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/url_slug_generator",
        json={"text": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_url_slug_generator_whitespace_only_returns_400(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/url_slug_generator",
        json={"text": "     "},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_url_slug_generator_over_max_length_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/url_slug_generator",
        json={"text": "a" * 501},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_url_slug_generator_no_sluggable_characters_returns_400(client, api_key):
    """Corrupted/degenerate-input failure path distinct from the empty/
    whitespace case: valid non-whitespace text that nonetheless contains
    no characters that can form a slug (only punctuation)."""
    resp = await client.post(
        "/v1/seo_tools/url_slug_generator",
        json={"text": "!!!???..."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_url_slug_generator_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(seo_tools_router, "generate_slug", _boom)
    resp = await client.post(
        "/v1/seo_tools/url_slug_generator",
        json={"text": "Hello World"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to generate slug"
    assert body["error"]["code"] == "SLUG_GENERATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text


# ---------------------------------------------------------------------------
# /content_readability_analyzer
# ---------------------------------------------------------------------------

_READABILITY_TEXT = (
    "The quick brown fox jumps over the lazy dog. This sentence is used "
    "to test readability scoring. It contains several words and multiple "
    "sentences for analysis."
)


async def test_content_readability_analyzer_valid_request_returns_200_with_correct_shape(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/content_readability_analyzer",
        json={"text": _READABILITY_TEXT},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for field in (
        "text",
        "word_count",
        "sentence_count",
        "syllable_count",
        "flesch_reading_ease",
        "flesch_kincaid_grade",
        "difficulty",
    ):
        assert field in body
    assert body["word_count"] > 0
    assert body["sentence_count"] > 0
    assert body["syllable_count"] > 0
    assert isinstance(body["difficulty"], str)


async def test_content_readability_analyzer_missing_field_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/content_readability_analyzer",
        json={},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_content_readability_analyzer_empty_string_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/content_readability_analyzer",
        json={"text": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_content_readability_analyzer_under_min_length_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/content_readability_analyzer",
        json={"text": "too short"},  # < 20 chars
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_content_readability_analyzer_whitespace_only_returns_400(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/content_readability_analyzer",
        json={"text": " " * 25},  # satisfies min_length=20 but is whitespace-only
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_content_readability_analyzer_over_max_length_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/content_readability_analyzer",
        json={"text": "a" * 10_001},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_content_readability_analyzer_no_words_returns_400(client, api_key):
    """Corrupted/degenerate-input failure path: satisfies both min_length
    and non-whitespace, but contains no recognizable words."""
    resp = await client.post(
        "/v1/seo_tools/content_readability_analyzer",
        json={"text": "1234567890 !@#$%^&*() 1234567890"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_content_readability_analyzer_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(seo_tools_router, "analyze_readability", _boom)
    resp = await client.post(
        "/v1/seo_tools/content_readability_analyzer",
        json={"text": _READABILITY_TEXT},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to analyze readability"
    assert body["error"]["code"] == "READABILITY_ANALYSIS_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text


# ---------------------------------------------------------------------------
# /social_media_tags_generator
# ---------------------------------------------------------------------------

async def test_social_media_tags_generator_valid_request_returns_200_with_correct_shape(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/social_media_tags_generator",
        json={
            "title": "My Article",
            "description": "My article description.",
            "image_url": "https://example.com/image.png",
            "url": "https://example.com/article",
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == "My Article"
    assert body["description"] == "My article description."
    assert body["image_url"] == "https://example.com/image.png"
    assert body["url"] == "https://example.com/article"
    snippet = body["html_snippet"]
    assert 'property="og:title" content="My Article"' in snippet
    assert 'property="og:image" content="https://example.com/image.png"' in snippet
    assert 'property="og:url" content="https://example.com/article"' in snippet
    assert 'name="twitter:card" content="summary_large_image"' in snippet


async def test_social_media_tags_generator_url_optional_omitted_from_snippet(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/social_media_tags_generator",
        json={
            "title": "My Article",
            "description": "My article description.",
            "image_url": "https://example.com/image.png",
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"] is None
    assert "og:url" not in body["html_snippet"]


async def test_social_media_tags_generator_missing_image_url_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/social_media_tags_generator",
        json={"title": "My Article", "description": "desc"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_social_media_tags_generator_empty_title_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/social_media_tags_generator",
        json={"title": "", "description": "desc", "image_url": "https://example.com/i.png"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_social_media_tags_generator_whitespace_only_description_returns_400(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/social_media_tags_generator",
        json={"title": "My Article", "description": "   ", "image_url": "https://example.com/i.png"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_social_media_tags_generator_image_url_over_max_length_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/social_media_tags_generator",
        json={
            "title": "My Article",
            "description": "desc",
            "image_url": "https://example.com/" + ("a" * 2001),
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_social_media_tags_generator_description_very_long_returns_422_not_500(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/social_media_tags_generator",
        json={
            "title": "My Article",
            "description": "a" * 10_050,
            "image_url": "https://example.com/i.png",
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_social_media_tags_generator_xss_payloads_are_html_escaped_in_snippet(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/social_media_tags_generator",
        json={
            "title": _SCRIPT_PAYLOAD,
            "description": _BREAKOUT_PAYLOAD,
            "image_url": _SCRIPT_PAYLOAD + _BREAKOUT_PAYLOAD,
            "url": _BREAKOUT_PAYLOAD + _SCRIPT_PAYLOAD,
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    snippet = body["html_snippet"]

    # The raw, unescaped attack strings must never appear anywhere in the
    # generated HTML snippet, across all four fields (title, description,
    # image_url, url).
    assert "<script>" not in snippet
    assert "</script>" not in snippet
    assert '"><img' not in snippet

    # The HTML-escaped forms must be present instead.
    assert "&lt;script&gt;" in snippet
    assert "&quot;&gt;" in snippet
    assert html.escape(_SCRIPT_PAYLOAD) in snippet
    assert html.escape(_BREAKOUT_PAYLOAD) in snippet

    # Note: the raw echoed `title`/`description`/`image_url`/`url` fields in
    # the response body are expected to contain the unescaped payload
    # verbatim (they are plain data fields, not HTML) - only `html_snippet`
    # is an HTML-context field and must be escaped. The `snippet`-scoped
    # assertions above are the actual security check.


async def test_social_media_tags_generator_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(seo_tools_router, "generate_social_media_tags", _boom)
    resp = await client.post(
        "/v1/seo_tools/social_media_tags_generator",
        json={"title": "My Article", "description": "desc", "image_url": "https://example.com/i.png"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to generate social media tags"
    assert body["error"]["code"] == "SOCIAL_TAGS_GENERATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text


# ---------------------------------------------------------------------------
# /serp_preview
# ---------------------------------------------------------------------------

async def test_serp_preview_valid_request_returns_200_with_correct_shape(client, api_key):
    title = "A" * 40  # under the 60-char limit
    description = "B" * 100  # under the 160-char limit
    resp = await client.post(
        "/v1/seo_tools/serp_preview",
        json={"title": title, "description": description, "url": "https://example.com/page"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title"] == title
    assert body["description"] == description
    assert body["url"] == "https://example.com/page"
    assert body["title_length"] == 40
    assert body["description_length"] == 100
    assert body["title_exceeds_limit"] is False
    assert body["description_exceeds_limit"] is False


async def test_serp_preview_exceeds_limit_flags_true_over_threshold(client, api_key):
    title = "A" * 61
    description = "B" * 161
    resp = await client.post(
        "/v1/seo_tools/serp_preview",
        json={"title": title, "description": description, "url": "https://example.com/page"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["title_exceeds_limit"] is True
    assert body["description_exceeds_limit"] is True


async def test_serp_preview_missing_url_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/serp_preview",
        json={"title": "Title", "description": "Description"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_serp_preview_empty_description_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/serp_preview",
        json={"title": "Title", "description": "", "url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_serp_preview_whitespace_only_title_returns_400(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/serp_preview",
        json={"title": "     ", "description": "Description", "url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_serp_preview_url_over_max_length_returns_422(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/serp_preview",
        json={
            "title": "Title",
            "description": "Description",
            "url": "https://example.com/" + ("a" * 2001),
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_serp_preview_very_long_description_returns_422_not_500(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/serp_preview",
        json={"title": "Title", "description": "a" * 10_050, "url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_serp_preview_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(seo_tools_router, "analyze_serp_preview", _boom)
    resp = await client.post(
        "/v1/seo_tools/serp_preview",
        json={"title": "Title", "description": "Description", "url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to build SERP preview"
    assert body["error"]["code"] == "SERP_PREVIEW_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
