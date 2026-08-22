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
# Loan: extreme-input overflow fix (api.pdfconverterai.com#49)
#
# `calculate_loan()` used to let `(1 + monthly_rate) ** months` blow past
# Python float range for extreme `annual_rate`/`years` combinations (e.g.
# 2000%/1000 years), raising an unhandled `OverflowError` that the router's
# `except Exception` branch turned into an opaque 500
# ("Failed to calculate loan" with no indication it was a bad-input problem).
# The fix adds an explicit `annual_rate > 500 or years > 50` bounds check
# ahead of the exponentiation, raising the same client-safe `ValueError` ->
# 400 path already covered above for negative/zero inputs. These tests
# confirm the exact reported overflow case now fails clean (400, not 500),
# pin down the inclusive boundary the fix intends (500/50 still succeed;
# one unit past either edge is rejected), and confirm a realistic in-range
# mortgage calculation is unaffected.
# ---------------------------------------------------------------------------

async def test_loan_reported_overflow_case_returns_400_not_500(client, api_key):
    """The exact inputs from the bug report (annual_rate=2000, years=1000)
    used to overflow `(1 + monthly_rate) ** months` inside
    `calculate_loan()` and surface as an unhandled 500. Must now be a clean
    400 with a client-safe message."""
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 200000, "annual_rate": 2000, "years": 1000},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert "OverflowError" not in resp.text
    assert (
        body.get("message")
        == "Annual rate must be between 0 and 500%, and years must be between 1 and 50"
    )


async def test_loan_annual_rate_and_years_at_the_inclusive_boundary_still_succeed(
    client, api_key
):
    """annual_rate=500 and years=50 are the documented inclusive maximums
    (the check is `> 500` / `> 50`, not `>= `) - confirms the boundary
    itself is not accidentally rejected, and that the resulting
    exponentiation at the very edge still stays within float range."""
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 1000, "annual_rate": 500, "years": 50},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["monthly_payment"] > 0
    assert body["monthly_payment"] == pytest.approx(416.67, abs=0.01)


async def test_loan_annual_rate_just_over_the_boundary_returns_400(client, api_key):
    """500.01% - one hundredth of a percent past the inclusive 500% max -
    must be rejected, confirming the boundary is really `> 500`."""
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 1000, "annual_rate": 500.01, "years": 50},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_loan_years_just_over_the_boundary_returns_400(client, api_key):
    """51 years - one past the inclusive 50-year max - must be rejected,
    confirming the boundary is really `> 50`."""
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 1000, "annual_rate": 500, "years": 51},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text


async def test_loan_typical_mortgage_returns_a_sane_monthly_payment(client, api_key):
    """A normal-range, realistic 30-year mortgage (principal=200000,
    annual_rate=6.5%, years=30) - well clear of the new bounds check -
    confirms the fix didn't regress the ordinary case, and pins down the
    actual value (not just "didn't crash") using the standard amortization
    formula."""
    resp = await client.post(
        "/v1/calculators/loan",
        json={"principal": 200000, "annual_rate": 6.5, "years": 30},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["monthly_payment"] == 1264.14
    assert body["total_interest"] == 255088.98
    # Sanity bounds a regression in either direction would violate: a
    # monthly payment on a 30-year mortgage should be a small multiple of
    # principal/months, not near-zero or absurdly large.
    assert 1000 < body["monthly_payment"] < 2000


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
