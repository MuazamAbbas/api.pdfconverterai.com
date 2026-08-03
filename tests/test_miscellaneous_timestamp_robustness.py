"""Coverage for the error-message-leak fix applied to `app/routers/
miscellaneous.py`'s `get_timestamp` endpoint (`GET /v1/miscellaneous/
timestamp`) as part of issue #31 Batch 1/3 (see `tests/
test_cyber_security_robustness.py` module docstring for the full lineage:
#27/PR#28 -> #29/PR#30 -> #31).

Kept as its own file rather than folded into `tests/
test_miscellaneous_qr_code.py`, which is scoped specifically to the
`/qr_code` endpoint (per that file's own module docstring) and deliberately
untouched by this batch (`generate_qr_code` was already fixed in a prior
session, out of scope here).

`get_timestamp`'s first `logger.debug(...)` call happens before its `try`
block (same shape as `list_categories`/`list_tools`), but this endpoint
does have one risky inline expression inside `try`:
`datetime.utcnow().timestamp()`. The forced-500 test shadows the
module-global `datetime` name (imported via `from datetime import
datetime`, so it's a plain module attribute, unlike the C-extension
`datetime.datetime` class itself which does not allow arbitrary attribute
assignment) with a stand-in whose `.utcnow()` raises - a more realistic
failure simulation than the logger-message-content trick used for
categories/tools, and scoped exclusively to the code inside `try`.
"""
import pytest

import app.routers.miscellaneous as miscellaneous_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


class _BoomDatetime:
    @staticmethod
    def utcnow():
        raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/miscellaneous.py")


async def test_get_timestamp_valid_request_returns_200_with_integer_timestamp(client, api_key):
    resp = await client.get(
        "/v1/miscellaneous/timestamp",
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["timestamp"], int)
    assert body["timestamp"] > 0


async def test_get_timestamp_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(miscellaneous_router, "datetime", _BoomDatetime)
    resp = await client.get(
        "/v1/miscellaneous/timestamp",
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to generate timestamp"
    assert body["error"]["code"] == "TIMESTAMP_GENERATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "miscellaneous.py" not in resp.text
