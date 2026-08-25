"""Coverage for the error-message-leak fix applied to `app/routers/web_tools.py`
as part of issue #31 batch 3 - the fast-follow to batches 1 and 2 (`text.py`,
`miscellaneous.py`, `cyber_security.py`, `binary_tools.py`, `categories.py`,
`tools.py`, `seo_tools.py`, `ai_tools.py`, `video.py`), which fixed the
identical pattern (raw exception text / `str(e)` leaking into the response
body) across those routers.

Same technique as `tests/test_calculators_robustness.py`: force a genuine
exception through each handler's own real `except` branch (not just assert
`api_error()`'s return shape in isolation), plant a fake secret marker in the
forced exception's message, and confirm it never reaches the response body.

`/url_encode`'s `except Exception` branch now raises
`api_error(500, "Failed to encode URL", "URL_ENCODE_FAILED")` instead of the
old `HTTPException(status_code=500, detail=f"Error encoding URL: {str(e)}")`.

`/validate_url` has three except branches with different treatment:
- `aiohttp.ClientResponseError` (the target URL itself responded with an
  error status) and `aiohttp.ClientError` (the target URL was unreachable)
  are both legitimate check *results*, not internal failures - they still
  return HTTP 200 with `is_valid: false`, but the raw `str(e)` is now
  replaced with a generic, non-interpolated message.
- The generic `except Exception` branch is a genuine unexpected internal
  failure, not a valid "URL check result" - it now raises
  `api_error(500, "Failed to validate URL", "URL_VALIDATION_FAILED")`
  instead of returning a leaking HTTP 200 dict. This is the regression test
  proving the fix: before the fix this returned 200 with the marker leaked
  in the `error` field.
"""
import urllib.parse

import aiohttp
import pytest

import app.routers.web_tools as web_tools_router

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _boom(*args, **kwargs):
    raise RuntimeError(
        "SECRET_MARKER_never_reach_the_client db_password=hunter2 at "
        "/app/routers/web_tools.py"
    )


def _make_client_response_error(status: int) -> aiohttp.ClientResponseError:
    request_info = aiohttp.RequestInfo(
        url="http://example.com",
        method="GET",
        headers={},
        real_url="http://example.com",
    )
    return aiohttp.ClientResponseError(
        request_info=request_info,
        history=(),
        status=status,
        message="SECRET_MARKER_never_reach_the_client_response_error db_password=hunter2",
    )


# ---------------------------------------------------------------------------
# /url_encode
# ---------------------------------------------------------------------------

async def test_url_encode_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/web_tools/url_encode",
        json={"url": "https://example.com/a b?x=1&y=2"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["encoded_url"] == urllib.parse.quote("https://example.com/a b?x=1&y=2")


async def test_url_encode_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(urllib.parse, "quote", _boom)
    resp = await client.post(
        "/v1/web_tools/url_encode",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to encode URL"
    assert body["error"]["code"] == "URL_ENCODE_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "web_tools.py" not in resp.text


# ---------------------------------------------------------------------------
# /url_decode
# ---------------------------------------------------------------------------

async def test_url_decode_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/web_tools/url_decode",
        json={"url": urllib.parse.quote("https://example.com/a b?x=1&y=2")},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["decoded_url"] == "https://example.com/a b?x=1&y=2"


async def test_url_decode_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(urllib.parse, "unquote", _boom)
    resp = await client.post(
        "/v1/web_tools/url_decode",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to decode URL"
    assert body["error"]["code"] == "URL_DECODE_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "web_tools.py" not in resp.text


# ---------------------------------------------------------------------------
# /validate_url
# ---------------------------------------------------------------------------

async def test_validate_url_valid_request_returns_200_with_is_valid_true(
    client, api_key, monkeypatch
):
    async def _fake_check_url(session, url):
        return True, 200

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await client.post(
        "/v1/web_tools/validate_url",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_valid"] is True
    assert body["status_code"] == 200


async def test_validate_url_client_response_error_returns_200_with_generic_error(
    client, api_key, monkeypatch
):
    async def _fake_check_url(session, url):
        raise _make_client_response_error(404)

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await client.post(
        "/v1/web_tools/validate_url",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_valid"] is False
    assert body["status_code"] == 404
    assert body["error"] == "The URL returned an error response"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text


async def test_validate_url_client_error_returns_200_with_generic_error(
    client, api_key, monkeypatch
):
    async def _fake_check_url(session, url):
        raise aiohttp.ClientOSError(
            "SECRET_MARKER_never_reach_the_client_error db_password=hunter2"
        )

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await client.post(
        "/v1/web_tools/validate_url",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["is_valid"] is False
    assert body["status_code"] is None
    assert body["error"] == "Unable to reach the URL"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text


async def test_validate_url_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    async def _fake_check_url(session, url):
        _boom()

    monkeypatch.setattr(web_tools_router, "check_url", _fake_check_url)

    resp = await client.post(
        "/v1/web_tools/validate_url",
        json={"url": "https://example.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to validate URL"
    assert body["error"]["code"] == "URL_VALIDATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "web_tools.py" not in resp.text
