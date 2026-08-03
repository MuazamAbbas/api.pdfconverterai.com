"""Coverage for the error-message-leak fix applied to `app/routers/
tools.py`'s `list_tools` endpoint (`GET /v1/tools/list`) as part of issue
#31 Batch 1/3 (see `tests/test_cyber_security_robustness.py` module
docstring for the full lineage: #27/PR#28 -> #29/PR#30 -> #31).

This file was previously an empty placeholder.

Same structure and same reasoning as `tests/test_categories.py` (also
patches `logger.debug` conditionally on the "✅ ..." success message, for
the identical reason: the module's first `logger.debug` call happens
before the `try` block, and there is no separately-importable service
function or risky inline expression to target instead). Also confirms, as
a side effect, that the previously-latent missing-`HTTPException`-import
dead code in this router's `except Exception` branch is now gone.
"""
import pytest

import app.routers.tools as tools_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


def _boom_on_success_message(msg, *args, **kwargs):
    if isinstance(msg, str) and msg.startswith("✅"):
        raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/tools.py")
    return None


async def test_list_tools_valid_request_returns_200_with_tools(client, api_key):
    resp = await client.get(
        "/v1/tools/list",
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["tools"], dict)
    assert "pdf" in body["tools"]


async def test_list_tools_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(tools_router.logger, "debug", _boom_on_success_message)
    resp = await client.get(
        "/v1/tools/list",
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to list tools"
    assert body["error"]["code"] == "TOOLS_LIST_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "tools.py" not in resp.text
