"""Coverage for the error-message-leak fix applied to 6 of `app/routers/
text.py`'s endpoints (`/test`, `/grammar`, `/word_count`, `/char_count`,
`/sentence_count`, `/paragraph_count`) as part of issue #31 Batch 1/3 (see
`tests/test_cyber_security_robustness.py` module docstring for the full
lineage: #27/PR#28 -> #29/PR#30 -> #31). `/upload`, `/paraphrase`, and
`/summarize` already used `api_error` correctly before this batch and are
untouched here.

`word_count_endpoint`/`char_count_endpoint`/`sentence_count_endpoint`/
`paragraph_count_endpoint` each call a directly-importable service function
from `app.services.text.count`, so their forced-500 tests monkeypatch that
function reference on the router module itself, same as calculators.py/
seo_tools.py. `test_text` has no separately-importable service call - its whole body is
inline (`TextResponse(...)` then `json.dumps(response.dict())`), entirely
inside its own `try` block (no earlier `logger.debug` call to worry
about). The forced-500 test can't patch `text_router.json.dumps` directly:
`text_router.json` is the real, process-wide `json` module (not a
module-local copy), so patching its `dumps` attribute leaks into every
other `json.dumps` call in the request/response pipeline - including the
test app's own `JSONResponse` serialization of the resulting error
envelope, which made the test fail with a second, unrelated exception
instead of exercising the router's `except Exception` branch cleanly.
Patching `text_router.TextResponse` itself (the class the router
constructs, scoped to this module only) avoids that blast radius.

`grammar` gets three cases per the task brief, not two, because this batch
pairs a router-level fix (Part B: `text.py`'s `except Exception` branch
now uses `api_error`) with a service-level fix (Part C:
`app/services/text/grammar.py`'s `correct_grammar()` no longer mislabels
unexpected internal failures as a leaky `ValueError`/400 - it now bare
re-raises, matching `app/services/seo/keyword_extract.py`'s
`extract_keywords_service()`):

1. `test_grammar_too_short_text_returns_400_with_safe_validation_message` -
   regression check that Part C's fix to `grammar.py` didn't touch the two
   real, still-desired `ValueError`/400s for empty/too-short text (raised
   directly, before `grammar.py`'s own `try` block, untouched by this
   batch).
2. `test_grammar_unexpected_internal_failure_returns_500_without_leaking_
   exception_text` - the regression test proving Part C's fix works: an
   unexpected internal failure (here, `TextBlob` itself raising inside
   `correct_grammar`'s `try` block, monkeypatched via `app.services.text.
   grammar.TextBlob`, not via the router) must now propagate as its real
   exception type, get caught by the *router's* `except Exception` branch
   (Part B's fix), and return the generic 500 `api_error` envelope with the
   planted secret absent - not the old leaky `ValueError` -> 400 path.
3. `test_grammar_valid_request_returns_200_with_corrections` - normal
   happy path, exercising the real `TextBlob` end-to-end.
"""
import pytest

import app.routers.text as text_router
import app.services.text.grammar as grammar_service

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


async def _boom(*args, **kwargs):
    raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/text.py")


class _BoomTextResponse:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/text.py")


class _BoomTextBlob:
    def __init__(self, *args, **kwargs):
        raise RuntimeError(f"{_SECRET_MARKER} at /app/services/text/grammar.py")


# ---------------------------------------------------------------------------
# /test
# ---------------------------------------------------------------------------

async def test_test_text_valid_request_returns_200(client, api_key):
    resp = await client.get(
        "/v1/text/test",
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"]["message"] == "Text Tools router is working"


async def test_test_text_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(text_router, "TextResponse", _BoomTextResponse)
    resp = await client.get(
        "/v1/text/test",
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Test endpoint failed"
    assert body["error"]["code"] == "TEXT_PROCESSING_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "text.py" not in resp.text


# ---------------------------------------------------------------------------
# /grammar
# ---------------------------------------------------------------------------

async def test_grammar_too_short_text_returns_400_with_safe_validation_message(client, api_key):
    resp = await client.post(
        "/v1/text/grammar",
        json={"text": "short"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Text must be at least 10 characters"


async def test_grammar_unexpected_internal_failure_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(grammar_service, "TextBlob", _BoomTextBlob)
    resp = await client.post(
        "/v1/text/grammar",
        json={"text": "This is long enough text to pass validation."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to correct grammar"
    assert body["error"]["code"] == "TEXT_PROCESSING_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "grammar.py" not in resp.text


async def test_grammar_valid_request_returns_200_with_corrections(client, api_key):
    resp = await client.post(
        "/v1/text/grammar",
        json={"text": "This is a completely correct sentence."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["corrections"], list)


# ---------------------------------------------------------------------------
# /word_count
# ---------------------------------------------------------------------------

async def test_word_count_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/text/word_count",
        json={"text": "one two three"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["word_count"] == 3


async def test_word_count_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(text_router, "word_count", _boom)
    resp = await client.post(
        "/v1/text/word_count",
        json={"text": "one two three"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to count words"
    assert body["error"]["code"] == "TEXT_PROCESSING_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "text.py" not in resp.text


# ---------------------------------------------------------------------------
# /char_count
# ---------------------------------------------------------------------------

async def test_char_count_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/text/char_count",
        json={"text": "hello"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["char_count"] == 5


async def test_char_count_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(text_router, "char_count", _boom)
    resp = await client.post(
        "/v1/text/char_count",
        json={"text": "hello"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to count characters"
    assert body["error"]["code"] == "TEXT_PROCESSING_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "text.py" not in resp.text


# ---------------------------------------------------------------------------
# /sentence_count
# ---------------------------------------------------------------------------

async def test_sentence_count_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/text/sentence_count",
        json={"text": "One. Two. Three."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["sentence_count"] == 3


async def test_sentence_count_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(text_router, "sentence_count", _boom)
    resp = await client.post(
        "/v1/text/sentence_count",
        json={"text": "One. Two. Three."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to count sentences"
    assert body["error"]["code"] == "TEXT_PROCESSING_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "text.py" not in resp.text


# ---------------------------------------------------------------------------
# /paragraph_count
# ---------------------------------------------------------------------------

async def test_paragraph_count_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/text/paragraph_count",
        json={"text": "Para one.\nPara two."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"]["paragraph_count"] == 2


async def test_paragraph_count_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(text_router, "paragraph_count", _boom)
    resp = await client.post(
        "/v1/text/paragraph_count",
        json={"text": "Para one.\nPara two."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to count paragraphs"
    assert body["error"]["code"] == "TEXT_PROCESSING_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "text.py" not in resp.text
