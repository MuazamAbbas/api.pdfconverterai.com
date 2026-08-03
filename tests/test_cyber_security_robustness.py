"""Coverage for the error-message-leak fix applied to `app/routers/
cyber_security.py`'s `password_generator` endpoint (`GET /v1/cyber_security/
password_generator`) as part of issue #31 Batch 1/3 - the codebase-wide
sweep of the same `raise HTTPException(status_code=500, detail=f"...:
{str(e)}")` pattern already fixed for Unit Converters (#27/PR#28) and
Calculators (#29/PR#30).

Same technique as `tests/test_calculators_robustness.py`: force a genuine
exception through the endpoint's own real `except Exception` branch, plant
a fake secret marker in the forced exception's message, and confirm it
never reaches the response body - only the new safe
`api_error(500, "Failed to generate password", "PASSWORD_GENERATION_FAILED")`
envelope does.

`password_generator` has no separately-importable service function to
monkeypatch (unlike calculators.py) - its whole body is inline. The only
call inside its `try` block that can realistically fail is
`secrets.choice(characters)`, so the forced-500 test patches `secrets.choice`
(via the router module's own `secrets` reference, restored automatically by
`monkeypatch` at teardown) rather than a router-level helper function.
"""
import pytest

import app.routers.cyber_security as cyber_security_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


def _boom_choice(*args, **kwargs):
    raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/cyber_security.py")


async def test_password_generator_valid_request_returns_200_with_correct_length(client, api_key):
    resp = await client.get(
        "/v1/cyber_security/password_generator",
        params={"length": 16},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["password"]) == 16


async def test_password_generator_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(cyber_security_router.secrets, "choice", _boom_choice)
    resp = await client.get(
        "/v1/cyber_security/password_generator",
        params={"length": 16},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to generate password"
    assert body["error"]["code"] == "PASSWORD_GENERATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "cyber_security.py" not in resp.text
