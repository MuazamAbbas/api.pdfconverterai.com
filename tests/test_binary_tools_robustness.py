"""Coverage for the error-message-leak fix applied to `app/routers/
binary_tools.py`'s `text_to_binary` endpoint (`POST /v1/binary_tools/
text_to_binary`) as part of issue #31 Batch 1/3 (see `tests/
test_cyber_security_robustness.py` module docstring for the full lineage:
#27/PR#28 -> #29/PR#30 -> #31).

`text_to_binary` also has no separately-importable service function to
monkeypatch - its whole body is inline (`format(ord(char), '08b') for char
in request.text`). The forced-500 test shadows the module-global `ord`
name (Python's `LOAD_GLOBAL` for a bare name inside a function checks the
function's own module `__dict__` before falling back to builtins, so
setting `binary_tools.ord` on the module intercepts every `ord(...)` call
inside `text_to_binary`'s `try` block without touching the real builtin
used anywhere else) - the same spirit as `test_cyber_security_robustness.
py`'s `secrets.choice` patch, adapted for a builtin with no importable
owner object.
"""
import pytest

import app.routers.binary_tools as binary_tools_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


def _boom_ord(*args, **kwargs):
    raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/binary_tools.py")


async def test_text_to_binary_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": "Hi"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["binary"] == "01001000 01101001"


async def test_text_to_binary_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(binary_tools_router, "ord", _boom_ord, raising=False)
    resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": "Hi"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to convert text to binary"
    assert body["error"]["code"] == "BINARY_CONVERSION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "binary_tools.py" not in resp.text
