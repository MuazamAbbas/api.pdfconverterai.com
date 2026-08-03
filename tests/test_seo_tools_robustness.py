"""Coverage for the error-message-leak fix applied to both `app/routers/
seo_tools.py` endpoints (`POST /v1/seo_tools/keyword_density`, `POST
/v1/seo_tools/keyword_extract`) as part of issue #31 Batch 1/3 (see `tests/
test_cyber_security_robustness.py` module docstring for the full lineage:
#27/PR#28 -> #29/PR#30 -> #31).

Same technique as `tests/test_calculators_robustness.py`: each endpoint
calls a directly-importable service function (`keyword_density`,
`extract_keywords_service`), so the forced-500 tests monkeypatch that
function reference on the router module itself, exactly like calculators.py.
"""
import pytest

import app.routers.seo_tools as seo_tools_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


async def _boom(*args, **kwargs):
    raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/seo_tools.py")


# ---------------------------------------------------------------------------
# /keyword_density
# ---------------------------------------------------------------------------

async def test_keyword_density_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/keyword_density",
        json={"text": "the quick brown fox jumps over the lazy dog the fox runs"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert "keyword_density" in body
    assert "fox" in body["keyword_density"]


async def test_keyword_density_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(seo_tools_router, "keyword_density", _boom)
    resp = await client.post(
        "/v1/seo_tools/keyword_density",
        json={"text": "the quick brown fox jumps over the lazy dog the fox runs"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to calculate keyword density"
    assert body["error"]["code"] == "SEO_ANALYSIS_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "seo_tools.py" not in resp.text


# ---------------------------------------------------------------------------
# /keyword_extract
# ---------------------------------------------------------------------------

async def test_keyword_extract_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/seo_tools/keyword_extract",
        json={"text": "Machine learning models require large amounts of quality training data."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body["keywords"], list)
    assert len(body["keywords"]) > 0


async def test_keyword_extract_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(seo_tools_router, "extract_keywords_service", _boom)
    resp = await client.post(
        "/v1/seo_tools/keyword_extract",
        json={"text": "Machine learning models require large amounts of quality training data."},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to extract keywords"
    assert body["error"]["code"] == "SEO_ANALYSIS_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "seo_tools.py" not in resp.text
