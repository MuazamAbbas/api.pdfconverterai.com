"""Coverage for the error-message-leak fix applied to all 5
`app/routers/calculators.py` endpoints (`/age`, `/percentage`, `/loan`,
`/bmi`, `/financial_plan`) as part of issue #29 - the fast-follow to
issue #27 / PR #28 (`tests/test_unit_converters_robustness.py`), which
fixed the identical pattern (`raise HTTPException(status_code=500,
detail=f"...: {str(e)}")` leaking raw exception text into the response
body) across all 18 Unit Converter endpoints.

Same technique as that file: force a genuine exception through each
handler's own real `except Exception` branch (not just assert
`api_error()`'s return shape in isolation), plant a fake secret marker in
the forced exception's message, and confirm it never reaches the response
body - only the new safe `api_error(500, "Failed to calculate <type>",
"CALCULATION_FAILED")` envelope does.

**`/financial_plan`'s extra wrinkle:** unlike the other 4 endpoints, its
handler takes `app: FastAPI = Depends(get_app)`, and `get_app()`
(`app/routers/calculators.py`) does `from app.main import app` *inside its
own body* - a fresh import lookup on every call, not a reference captured
once at decoration time. `app.main` cannot actually be imported in this
checkout (`ModuleNotFoundError: No module named 'apscheduler'` - see
`tests/conftest.py`'s module docstring, which is why `build_test_app()`
never imports the real `app.main` either). Monkeypatching
`app.routers.calculators.get_app` itself does **not** work around this:
`Depends(get_app)` in the function signature already captured a direct
reference to the original function object at module-import time, so
rebinding the module attribute `get_app` afterwards doesn't change what
FastAPI actually calls. Instead, both `/financial_plan` tests below inject
a fake `app.main` module into `sys.modules` (`monkeypatch.setitem`, so it's
undone automatically at teardown) - `from app.main import app` resolves
`app.main` via `sys.modules` first, so once a stub is registered there,
`get_app()`'s inner import finds the stub without ever touching the real,
currently-unimportable `app/main.py`. This mirrors
`tests/test_financial_plan_calculator.py`'s own `_fake_app()` stub
(`SimpleNamespace(state=SimpleNamespace(financial_sentiment_pipeline=...))`)
for the exact same reason: `FinancialPlanCalculator.calculate_financial_plan`
only ever touches `self.app.state.financial_sentiment_pipeline`, so a
`SimpleNamespace` satisfies it without needing a real FastAPI instance.

The forced-500 case for `/financial_plan` makes the stub's
`financial_sentiment_pipeline` callable raise directly - this is a
realistic failure mode (a broken/unloaded model pipeline), already covered
in isolation for the service class itself by
`test_financial_plan_calculator.py::test_sentiment_pipeline_failure_
propagates`; this file additionally confirms that failure propagates all
the way through the router's own `except Exception` branch, through the
real HTTP layer, into the new safe envelope - not the old leaking one.
"""
import sys
import types
from datetime import datetime
from types import SimpleNamespace

import pytest

import app.routers.calculators as calculators_router

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _boom(*args, **kwargs):
    raise RuntimeError(
        "SECRET_MARKER_never_reach_the_client db_password=hunter2 at "
        "/app/routers/calculators.py"
    )


def _inject_fake_app_main(monkeypatch, pipeline_fn):
    """Register a stub `app.main` module (with just a `.app` attribute
    carrying the `financial_sentiment_pipeline` the calculator needs) into
    `sys.modules`, so `get_app()`'s own `from app.main import app` resolves
    to it instead of the real, currently-unimportable `app/main.py`. See
    module docstring for why patching `get_app` itself doesn't work."""
    fake_main = types.ModuleType("app.main")
    fake_main.app = SimpleNamespace(
        state=SimpleNamespace(financial_sentiment_pipeline=pipeline_fn)
    )
    monkeypatch.setitem(sys.modules, "app.main", fake_main)


# ---------------------------------------------------------------------------
# /age
# ---------------------------------------------------------------------------

async def test_age_valid_request_returns_200_with_correct_result(client, api_key):
    birth_date = "2000-06-15"
    today = datetime.utcnow()
    birth = datetime.strptime(birth_date, "%Y-%m-%d")
    expected_age = today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))

    resp = await client.post(
        "/v1/calculators/age",
        json={"birth_date": birth_date},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["age"] == expected_age


async def test_age_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(calculators_router, "calculate_age", _boom)
    resp = await client.post(
        "/v1/calculators/age",
        json={"birth_date": "2000-06-15"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to calculate age"
    assert body["error"]["code"] == "CALCULATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "calculators.py" not in resp.text


# ---------------------------------------------------------------------------
# /percentage
# ---------------------------------------------------------------------------

async def test_percentage_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/calculators/percentage",
        json={"value": 50, "percentage": 10},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == 5.0


async def test_percentage_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(calculators_router, "calculate_percentage", _boom)
    resp = await client.post(
        "/v1/calculators/percentage",
        json={"value": 50, "percentage": 10},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to calculate percentage"
    assert body["error"]["code"] == "CALCULATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "calculators.py" not in resp.text


# ---------------------------------------------------------------------------
# /loan
# ---------------------------------------------------------------------------

async def test_loan_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 1000, "annual_rate": 5, "years": 2},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["monthly_payment"] == 43.87
    assert body["total_interest"] == 52.91


async def test_loan_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(calculators_router, "calculate_loan", _boom)
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 1000, "annual_rate": 5, "years": 2},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to calculate loan"
    assert body["error"]["code"] == "CALCULATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "calculators.py" not in resp.text


# ---------------------------------------------------------------------------
# /bmi
# ---------------------------------------------------------------------------

async def test_bmi_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/calculators/bmi",
        json={"weight_kg": 70, "height_m": 1.75},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["bmi"] == 22.86


async def test_bmi_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(calculators_router, "calculate_bmi", _boom)
    resp = await client.post(
        "/v1/calculators/bmi",
        json={"weight_kg": 70, "height_m": 1.75},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to calculate BMI"
    assert body["error"]["code"] == "CALCULATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "calculators.py" not in resp.text


# ---------------------------------------------------------------------------
# /financial_plan
# ---------------------------------------------------------------------------

async def test_financial_plan_valid_request_returns_200_with_correct_result(
    client, api_key, monkeypatch
):
    def _positive_pipeline(text):
        return [{"label": "POSITIVE", "score": 0.99}]

    _inject_fake_app_main(monkeypatch, _positive_pipeline)

    resp = await client.post(
        "/v1/calculators/financial_plan",
        json={
            "principal": 1000,
            "rate": 5,
            "years": 2,
            "market_sentiment": "great outlook",
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["investment_value"] == 1102.5
    assert body["sentiment_adjusted_value"] == 1157.62
    assert body["sentiment"] == "positive"


async def test_financial_plan_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    """Forces the failure inside the real `FinancialPlanCalculator.
    calculate_financial_plan`'s own try block (a broken sentiment pipeline -
    a realistic failure mode, already unit-tested in isolation by
    `test_financial_plan_calculator.py::test_sentiment_pipeline_failure_
    propagates`), and confirms it still propagates through the router's
    `except Exception` branch into the safe envelope end-to-end."""
    _inject_fake_app_main(monkeypatch, _boom)

    resp = await client.post(
        "/v1/calculators/financial_plan",
        json={
            "principal": 1000,
            "rate": 5,
            "years": 2,
            "market_sentiment": "great outlook",
        },
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to calculate financial plan"
    assert body["error"]["code"] == "CALCULATION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "calculators.py" not in resp.text
