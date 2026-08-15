"""Edge-case coverage for the BMI / Age / Percentage / Loan Calculators batch
(approved spec logged in docs/roadmap/SPRINT_STATUS.md, "2026-08-15 - Feature
spec approved: BMI / Age / Percentage / Loan Calculators batch").

`tests/test_calculators_robustness.py` already covers the happy path + the
error-leak (forced-500) path for all 5 `/calculators/*` endpoints. This file
fills in the specific gaps called out in that spec that weren't exercised
anywhere yet:

- Age: leap-year boundaries (birth on Feb 29), a same-day-birthday exact
  anniversary (months:0, days:0), and the future-date/invalid-format 400
  paths (previously only exercised via a forced monkeypatched 500, never the
  real `ValueError` -> 400 path).
- Percentage/Loan/BMI: the real (unmocked) negative/zero-input validation
  path returning 400 - previously only the happy path and the forced-500
  path were covered for these three, never the service's own `ValueError`
  branch.

`calculate_age()` (`app/services/calculators/age.py`) computes "today" via
its own module-level `datetime.utcnow()` call with no injection seam, so the
leap-year/same-day tests here freeze "today" by monkeypatching the `datetime`
name inside that module to a subclass whose `utcnow()` returns a fixed value
(same technique either works for direct unit calls or through the real HTTP
layer, since the router calls the same function object against the same
module globals).
"""
import calendar
from datetime import datetime as real_datetime

import pytest

import app.services.calculators.age as age_module
from app.services.calculators.age import calculate_age

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _freeze_today(monkeypatch, year, month, day):
    """Monkeypatch `app.services.calculators.age`'s own `datetime` name so
    `datetime.utcnow()` inside `calculate_age()` returns a fixed date, while
    `datetime.strptime()` (used to parse `birth_date`) keeps working
    normally - it's inherited straight from the real `datetime` class this
    subclasses."""

    class _FrozenDatetime(real_datetime):
        @classmethod
        def utcnow(cls):
            return cls(year, month, day)

    monkeypatch.setattr(age_module, "datetime", _FrozenDatetime)


# ---------------------------------------------------------------------------
# Age: leap-year boundaries
# ---------------------------------------------------------------------------

def test_age_leap_day_birth_evaluated_on_a_leap_year_anniversary(monkeypatch):
    """Born on a leap day (2000-02-29), evaluated on another leap year's
    Feb 29 (2024-02-29) - the day genuinely exists in both years, so this
    should be a clean 24y/0m/0d anniversary, no borrowing needed."""
    _freeze_today(monkeypatch, 2024, 2, 29)
    result = calculate_age("2000-02-29")
    assert result == {"years": 24, "months": 0, "days": 0}


def test_age_leap_day_birth_evaluated_on_a_non_leap_year(monkeypatch):
    """Born on a leap day (2000-02-29), evaluated on 2023-03-01 - 2023 has
    no Feb 29, so the algorithm has to borrow across the shorter (28-day)
    February. Confirms `calendar.monthrange` is doing real leap-year-aware
    day counting rather than assuming every February has 29 days."""
    _freeze_today(monkeypatch, 2023, 3, 1)
    result = calculate_age("2000-02-29")
    assert result == {"years": 23, "months": 0, "days": 0}


def test_age_leap_day_birth_evaluated_just_before_the_non_leap_anniversary(monkeypatch):
    """Same leap-day birth, but one day earlier (2023-02-28) - the
    anniversary hasn't "arrived" yet in the non-leap year, so this should
    still show 22 full years, not 23."""
    _freeze_today(monkeypatch, 2023, 2, 28)
    result = calculate_age("2000-02-29")
    assert result == {"years": 22, "months": 11, "days": 30}


# ---------------------------------------------------------------------------
# Age: same-day birthday (exact anniversary)
# ---------------------------------------------------------------------------

def test_age_same_day_birthday_is_a_clean_anniversary_with_zero_months_and_days(monkeypatch):
    _freeze_today(monkeypatch, 2026, 8, 15)
    result = calculate_age("2000-08-15")
    assert result == {"years": 26, "months": 0, "days": 0}


def test_age_same_day_birthday_one_day_before_anniversary_shows_11_months(monkeypatch):
    """Sanity check against the same-day case above: one calendar day
    earlier should not have rolled over into the new year yet."""
    _freeze_today(monkeypatch, 2026, 8, 14)
    result = calculate_age("2000-08-15")
    assert result == {"years": 25, "months": 11, "days": 30}


# ---------------------------------------------------------------------------
# Age: future-date rejection (unit-level and through the real HTTP layer)
# ---------------------------------------------------------------------------

def test_age_future_birth_date_raises_value_error(monkeypatch):
    _freeze_today(monkeypatch, 2026, 8, 15)
    with pytest.raises(ValueError, match="Birth date cannot be in the future"):
        calculate_age("2026-08-16")


def test_age_invalid_format_raises_value_error():
    with pytest.raises(ValueError, match="Invalid date format"):
        calculate_age("15-08-2000")


async def test_age_future_birth_date_returns_400_over_http_not_500(client, api_key, monkeypatch):
    """Exercises the real (unmocked) `ValueError` -> `HTTPException(400)`
    branch in the router - `test_calculators_robustness.py` only ever forces
    a generic 500 here, never this validation path."""
    _freeze_today(monkeypatch, 2026, 8, 15)
    resp = await client.post(
        "/v1/calculators/age",
        json={"birth_date": "2026-08-16"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["message"] == "Birth date cannot be in the future" or (
        "detail" in body and body["detail"] == "Birth date cannot be in the future"
    )


async def test_age_invalid_format_returns_400_over_http_not_500(client, api_key):
    resp = await client.post(
        "/v1/calculators/age",
        json={"birth_date": "not-a-date"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Percentage: zero/negative input validation (real, unmocked path)
# ---------------------------------------------------------------------------

async def test_percentage_zero_value_and_zero_percentage_are_accepted(client, api_key):
    """Zero is a valid, meaningful input here (0% of anything is 0; any % of
    0 is 0) - confirms the `< 0` check doesn't also reject `== 0`."""
    resp = await client.post(
        "/v1/calculators/percentage",
        json={"value": 0, "percentage": 0},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == 0


async def test_percentage_negative_value_returns_400_not_a_silent_bad_result(client, api_key):
    resp = await client.post(
        "/v1/calculators/percentage",
        json={"value": -50, "percentage": 10},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_percentage_negative_percentage_returns_400_not_a_silent_bad_result(client, api_key):
    resp = await client.post(
        "/v1/calculators/percentage",
        json={"value": 50, "percentage": -10},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# Loan: zero/negative input validation (real, unmocked path)
# ---------------------------------------------------------------------------

async def test_loan_zero_interest_rate_is_a_valid_input_not_rejected(client, api_key):
    """0% interest is a real, meaningful loan (e.g. a 0-APR promo) - the
    service's own `annual_rate < 0` check (not `<= 0`) is deliberate; this
    confirms it actually behaves that way end-to-end and the zero-rate
    branch in `calculate_loan()` produces a sane, non-error result."""
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 1200, "annual_rate": 0, "years": 1},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["monthly_payment"] == 100.0
    assert body["total_interest"] == 0


async def test_loan_zero_principal_returns_400(client, api_key):
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 0, "annual_rate": 5, "years": 2},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_loan_negative_principal_returns_400_not_a_silent_bad_result(client, api_key):
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": -1000, "annual_rate": 5, "years": 2},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_loan_negative_annual_rate_returns_400(client, api_key):
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 1000, "annual_rate": -5, "years": 2},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_loan_zero_years_returns_400(client, api_key):
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 1000, "annual_rate": 5, "years": 0},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


# ---------------------------------------------------------------------------
# BMI: zero/negative input validation (real, unmocked path)
# ---------------------------------------------------------------------------

async def test_bmi_zero_weight_returns_400_not_a_silent_bad_result(client, api_key):
    resp = await client.post(
        "/v1/calculators/bmi",
        json={"weight_kg": 0, "height_m": 1.75},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_bmi_negative_height_returns_400_not_a_silent_bad_result(client, api_key):
    resp = await client.post(
        "/v1/calculators/bmi",
        json={"weight_kg": 70, "height_m": -1.75},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


@pytest.mark.parametrize(
    "weight_kg,height_m,expected_bmi",
    [
        # Height chosen as exactly 1m so bmi == weight_kg, to hit the WHO
        # band boundaries (18.5 / 25 / 30) precisely - band *categorization*
        # itself is computed client-side (see
        # frontend/app/tools/unit-converter/bmi-calculator/page.tsx's
        # `categorizeBmi`), not by this backend, so this only confirms the
        # backend returns the exact boundary values the frontend bands on.
        (18.5, 1, 18.5),
        (25, 1, 25.0),
        (30, 1, 30.0),
    ],
)
async def test_bmi_returns_exact_value_at_who_band_boundaries(
    client, api_key, weight_kg, height_m, expected_bmi
):
    resp = await client.post(
        "/v1/calculators/bmi",
        json={"weight_kg": weight_kg, "height_m": height_m},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["bmi"] == expected_bmi
