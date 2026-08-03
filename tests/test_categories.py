"""Coverage for the error-message-leak fix applied to `app/routers/
categories.py`'s `list_categories` endpoint (`GET /v1/categories/list`) as
part of issue #31 Batch 1/3 (see `tests/test_cyber_security_robustness.py`
module docstring for the full lineage: #27/PR#28 -> #29/PR#30 -> #31).

This file was previously an empty placeholder.

`list_categories` builds a static list and has no separately-importable
service function, and unlike `text_to_binary`/`password_generator` it has
no risky inline expression either (a plain list literal can't fail) - the
only statement inside its `try` block that can be made to fail is its own
success-path `logger.debug("✅ Categories listed: %s", categories)` call.
The module's *first* `logger.debug(...)` call happens before the `try`
block, so a blanket patch of `logger.debug` would blow up outside the
`try`/`except` entirely and get caught by the test app's generic
`Exception` handler instead of the router's own (this batch's fix). The
forced-500 test instead patches `logger.debug` with a stand-in that only
raises for the specific "✅ ..." success message logged inside the `try`
block, so the earlier, outside-`try` debug call is unaffected.

This doctests as a side effect that `list_categories`'s previously-latent
`except Exception as e: raise HTTPException(...)` with no `HTTPException`
import (a dead-code `NameError` that could never actually trigger, because
nothing in the original `try` block could raise) is now gone - the branch
is exercised for the first time here and correctly returns the safe
`api_error` envelope.
"""
import pytest

import app.routers.categories as categories_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


def _boom_on_success_message(msg, *args, **kwargs):
    if isinstance(msg, str) and msg.startswith("✅"):
        raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/categories.py")
    return None


async def test_list_categories_valid_request_returns_200_with_categories(client, api_key):
    resp = await client.get(
        "/v1/categories/list",
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["categories"], list)
    assert "pdf" in body["categories"]


async def test_list_categories_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(categories_router.logger, "debug", _boom_on_success_message)
    resp = await client.get(
        "/v1/categories/list",
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to list categories"
    assert body["error"]["code"] == "CATEGORIES_LIST_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "categories.py" not in resp.text
