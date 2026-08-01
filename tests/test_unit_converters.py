"""Coverage for `POST /v1/unit_converters/length`,
`POST /v1/unit_converters/temperature`,
`POST /v1/unit_converters/weight`,
`POST /v1/unit_converters/area`,
`POST /v1/unit_converters/volume`,
`POST /v1/unit_converters/speed`,
`POST /v1/unit_converters/time`,
`POST /v1/unit_converters/data`,
`POST /v1/unit_converters/energy`,
`POST /v1/unit_converters/power`,
`POST /v1/unit_converters/pressure`,
`POST /v1/unit_converters/frequency`,
`POST /v1/unit_converters/force`,
`POST /v1/unit_converters/torque`,
`POST /v1/unit_converters/density`,
`POST /v1/unit_converters/flow_rate`,
`POST /v1/unit_converters/angle`, and
`POST /v1/unit_converters/fuel_efficiency` (Handbook Part I.2 - Tier 1, no
job queue, plain sync endpoints), plus their unit-validation and
`verify_api_key` auth behavior.

`GET /v1/unit_converters/test` is also covered as a smoke check that the
router mount itself still works.

`POST /v1/unit_converters/convert` (the NLP/contextual endpoint) is
intentionally out of scope - untouched this session.

Against the real `unit_converters` router (mounted in
`tests/conftest.py::build_test_app`) and real Mongo (via the `api_key`
fixture) - no mocking of `verify_api_key`, matching the rest of this test
suite's convention (see `tests/test_miscellaneous_qr_code.py`) of exercising
the real dependency against a real, fixture-created API key document rather
than overriding it.

Expected `result` values for length below are computed/measured, not loose
tolerances, per this repo's established testing standard - each is either an
exact real-world unit-conversion identity (1 mile = 1609.344 meters, 1 yard =
3 feet, 1 mile = 5280 feet, 1 foot = 12 inches, 1 kilometer = 1000 meters) or
independently computed from the router's own
`result = value * units[from_unit] / units[to_unit]` formula rounded to 4
decimal places (see `app/routers/unit_converters.py::convert_length`).

Temperature is NOT a multiplier-through-origin conversion like length -
Celsius/Fahrenheit/Kelvin are affine (offset-based: C->F is `x9/5 + 32`, C->K
is `+273.15`). Expected values below are the well-known reference identities
(0C = 32F = 273.15K, 100C = 212F = 373.15K, -40C = -40F = 233.15K - the point
where the Celsius and Fahrenheit scales cross) rather than router-formula
round-trips, since these are independently verifiable physical constants.

Weight IS a multiplier-through-origin conversion like length (see
`convert_weight`'s own `units` dict/formula) - exactly 4 units, confirmed
live on the v1 production site
(https://pdfconverterai.com/tools/unit-converter/weight-converter):
kilogram (base, 1.0), gram (0.001), pound (0.45359237), ounce
(0.028349523125). Expected values below are computed from that same
`result = value * units[from_unit] / units[to_unit]` formula, rounded to 4
decimal places.

Area IS also a multiplier-through-origin conversion like length/weight (see
`convert_area`'s own `units` dict/formula) - exactly 5 units: square_meter
(base, 1.0), square_kilometer (1_000_000.0), square_foot (0.09290304),
square_yard (0.83612736), acre (4046.8564224). Expected values below are
either exact real-world identities (1 acre = 43560 square feet, 1 square
yard = 9 square feet, since a yard is 3 feet on a side) or independently
computed from that same `result = value * units[from_unit] /
units[to_unit]` formula, rounded to 4 decimal places.

Volume IS also a multiplier-through-origin conversion like length/weight/area
(see `convert_volume`'s own `units` dict/formula) - exactly 5 units,
confirmed live against v1's Volume Converter page dropdown (Liters,
Milliliters, Cubic Meters, Gallons, Cubic Feet): liter (base, 1.0),
milliliter (0.001), cubic_meter (1000.0), gallon (3.785411784 - US gallon,
matching v1's stated "1 gal = 3.78541 L"), cubic_foot (28.316846592 - exact,
derived from 0.3048**3 m^3). Expected values below are independently
computed from that same `result = value * units[from_unit] /
units[to_unit]` formula, rounded to 4 decimal places (no clean
whole-number real-world identities exist between liter/gallon/cubic_foot
the way they do for length/weight/area's unit pairs).

Speed, time, data, and energy (batch 2) are ALL also multiplier-through-origin
conversions (see each `convert_*`'s own `units` dict/formula) - unit keys and
base-relative multipliers below match `app/routers/unit_converters.py`
exactly:
- speed: meter_per_second (base, 1.0), kilometer_per_hour (0.277777778),
  mile_per_hour (0.44704), knot (0.514444444).
- time: second (base, 1.0), minute (60.0), hour (3600.0), day (86400.0),
  week (604800.0).
- data: byte (base, 1.0), kilobyte (1024.0), megabyte (1024.0**2), gigabyte
  (1024.0**3), terabyte (1024.0**4) - binary (1024-based) multiples, not
  decimal (1000-based) SI ones.
- energy: joule (base, 1.0), kilojoule (1000.0), calorie (4.184), kilocalorie
  (4184.0), watt_hour (3600.0).

Expected values for all four below are independently computed from the same
`result = value * units[from_unit] / units[to_unit]` formula, rounded to 4
decimal places, matching this file's established convention.

Power, pressure, frequency, force, torque, density, flow_rate, and angle
(batch 3) are ALL also multiplier-through-origin conversions (see each
`convert_*`'s own `units` dict/formula in
`app/routers/unit_converters.py`) - unit keys and base-relative multipliers
below match the router exactly:
- power: watt (base, 1.0), kilowatt (1000.0), horsepower (745.699872),
  megawatt (1e6).
- pressure: pascal (base, 1.0), atmosphere (101325.0), bar (100000.0), psi
  (6894.757293168).
- frequency: hertz (base, 1.0), kilohertz (1e3), megahertz (1e6), gigahertz
  (1e9).
- force: newton (base, 1.0), kilonewton (1000.0), pound_force
  (4.4482216152605), kilogram_force (9.80665).
- torque: newton_meter (base, 1.0), foot_pound (1.3558179483314), inch_pound
  (0.1129848290276), kilogram_force_meter (9.80665).
- density: kg_per_cubic_meter (base, 1.0), g_per_cubic_cm (1000.0),
  lb_per_cubic_ft (16.018463374), kg_per_liter (1000.0).
- flow_rate: liter_per_second (base, 1.0), cubic_meter_per_hour
  (0.277777778), gallon_per_minute (0.0630901964), cubic_foot_per_minute
  (0.4719474432).
- angle: degree (0.017453292519943), radian (base, 1.0), gradian
  (0.015707963267949), turn (6.283185307179586).

Expected values for all eight below are independently computed from the
same `result = value * units[from_unit] / units[to_unit]` formula, rounded
to 4 decimal places, matching this file's established convention. Round-trip
tests use `pytest.approx` (not exact equality) since not every unit pair's
double-rounding cancels out cleanly at 4dp, mirroring the volume/area/weight
round-trip tests above.

Fuel efficiency (the 18th and final unit-converter sub-tool) is NOT a
multiplier-through-origin conversion like the batches above - it is
RECIPROCAL, mirroring temperature's affine from->base->to shape but with
division instead of an offset (see `convert_fuel_efficiency` in
`app/routers/unit_converters.py`). Three units: `mpg` (miles per US gallon),
`km_per_liter` (base, direct multiplier `MPG_TO_KPL = 0.425143707`), and
`l_per_100km` (reciprocal of km_per_liter: `100 / km_per_liter`, not a
multiplier). Expected values below are independently computed from that
same from->km_per_liter->to formula, rounded to 4 decimal places. Unlike
every batch above, `from_unit == to_unit` is handled as an explicit
identity short-circuit in the router (not a flat `units` dict divide-by-self),
and a `value == 0` request on any leg that divides BY the input value
(`l_per_100km` as `from_unit`, or any path landing on `l_per_100km` as
`to_unit` after conversion to a zero `km_per_liter`) is caught and returns
400 "Invalid value: cannot convert a value of zero for this unit" rather
than a 500 or an `inf`/`nan` leaking into the JSON body - a genuinely new
failure mode this suite has to cover that no earlier batch needed.
"""
import pytest

# See tests/test_worker_retry.py's module docstring/comment for why this is
# pinned to the session-scoped loop (Motor's shared `app.core.database.db`
# client).
pytestmark = pytest.mark.asyncio(loop_scope="session")


async def test_unit_converters_health_check_still_returns_200(client, api_key):
    resp = await client.get(
        "/v1/unit_converters/test",
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    assert resp.json() == {"message": "Unit Converters router is working"}


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 meter = 100 centimeters.
        pytest.param(1, "meter", "centimeter", 100.0, id="meter_to_centimeter"),
        # 100 centimeters = 1 meter (inverse of the above).
        pytest.param(100, "centimeter", "meter", 1.0, id="centimeter_to_meter"),
        # 1 kilometer = 1000 meters.
        pytest.param(1, "kilometer", "meter", 1000.0, id="kilometer_to_meter"),
        # 1 mile = 1609.344 meters (exact, by definition of the router's own
        # constant).
        pytest.param(1, "mile", "meter", 1609.344, id="mile_to_meter"),
        # 1 mile = 5280 feet (1609.344 / 0.3048 == 5280.0 exactly).
        pytest.param(1, "mile", "foot", 5280.0, id="mile_to_foot"),
        # 1 mile = 1760 yards (1609.344 / 0.9144 == 1760.0 exactly).
        pytest.param(1, "mile", "yard", 1760.0, id="mile_to_yard"),
        # 1 yard = 3 feet (0.9144 / 0.3048 == 3.0 exactly).
        pytest.param(1, "yard", "foot", 3.0, id="yard_to_foot"),
        # 1 foot = 12 inches (0.3048 / 0.0254 == 12.0 exactly).
        pytest.param(1, "foot", "inch", 12.0, id="foot_to_inch"),
        # 1 inch = 2.54 centimeters (0.0254 / 0.01 == 2.54 exactly).
        pytest.param(1, "inch", "centimeter", 2.54, id="inch_to_centimeter"),
        # 1 meter = 3.2808 feet (1 / 0.3048 == 3.280839895..., rounded to 4dp
        # per the router's own `round(result, 4)`).
        pytest.param(1, "meter", "foot", 3.2808, id="meter_to_foot_rounded"),
    ],
)
async def test_length_conversion_round_trips_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/length",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["meter", "foot", "inch", "kilometer", "centimeter", "mile", "yard"],
)
async def test_all_seven_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 7
    units as both a valid `from_unit` and a valid `to_unit` cheaply: any
    unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/length",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/length",
        json={"value": 1, "from_unit": "parsec", "to_unit": "meter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/length",
        json={"value": 1, "from_unit": "meter", "to_unit": "parsec"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_missing_value_field_rejected_with_422(client, api_key):
    """`LengthConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/length",
        json={"from_unit": "meter", "to_unit": "foot"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_invalid_api_key_value_rejected_with_envelope(client):
    """Mirrors
    `tests/test_miscellaneous_qr_code.py::test_qr_code_invalid_api_key_value_rejected_with_envelope`
    - a structurally present but unrecognized `X-API-Key` fails inside
    `verify_api_key` itself (`db.api_keys.find_one` returns nothing), which
    is a 403, not a 422."""
    resp = await client.post(
        "/v1/unit_converters/length",
        json={"value": 1, "from_unit": "meter", "to_unit": "foot"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_missing_api_key_header_rejected(client):
    """Mirrors
    `tests/test_miscellaneous_qr_code.py::test_qr_code_missing_api_key_header_rejected`
    - a structurally missing `X-API-Key` header fails FastAPI's own
    `Header(...)` requirement (a `RequestValidationError`), so it's a 422
    with the same envelope as an invalid request body, not `verify_api_key`'s
    403 (its function body never runs since the header parameter itself
    fails to resolve)."""
    resp = await client.post(
        "/v1/unit_converters/length",
        json={"value": 1, "from_unit": "meter", "to_unit": "foot"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # Freezing point of water.
        pytest.param(0, "celsius", "fahrenheit", 32.0, id="0c_to_32f"),
        pytest.param(32, "fahrenheit", "celsius", 0.0, id="32f_to_0c"),
        pytest.param(0, "celsius", "kelvin", 273.15, id="0c_to_273_15k"),
        pytest.param(273.15, "kelvin", "celsius", 0.0, id="273_15k_to_0c"),
        pytest.param(32, "fahrenheit", "kelvin", 273.15, id="32f_to_273_15k"),
        pytest.param(273.15, "kelvin", "fahrenheit", 32.0, id="273_15k_to_32f"),
        # Boiling point of water.
        pytest.param(100, "celsius", "fahrenheit", 212.0, id="100c_to_212f"),
        pytest.param(212, "fahrenheit", "celsius", 100.0, id="212f_to_100c"),
        pytest.param(100, "celsius", "kelvin", 373.15, id="100c_to_373_15k"),
        pytest.param(373.15, "kelvin", "celsius", 100.0, id="373_15k_to_100c"),
        pytest.param(212, "fahrenheit", "kelvin", 373.15, id="212f_to_373_15k"),
        pytest.param(373.15, "kelvin", "fahrenheit", 212.0, id="373_15k_to_212f"),
        # -40 is the point where the Celsius and Fahrenheit scales cross
        # (a non-trivial value distinct from the freezing/boiling anchors
        # above).
        pytest.param(-40, "celsius", "fahrenheit", -40.0, id="neg40c_to_neg40f"),
        pytest.param(-40, "fahrenheit", "celsius", -40.0, id="neg40f_to_neg40c"),
        pytest.param(-40, "celsius", "kelvin", 233.15, id="neg40c_to_233_15k"),
        pytest.param(233.15, "kelvin", "celsius", -40.0, id="233_15k_to_neg40c"),
        pytest.param(-40, "fahrenheit", "kelvin", 233.15, id="neg40f_to_233_15k"),
        pytest.param(233.15, "kelvin", "fahrenheit", -40.0, id="233_15k_to_neg40f"),
    ],
)
async def test_temperature_conversion_with_known_reference_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/temperature",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize("unit", ["celsius", "fahrenheit", "kelvin"])
async def test_all_three_temperature_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 3
    temperature units as both a valid `from_unit` and a valid `to_unit`
    cheaply: any unit not in the router's `valid_units` set would 400 with
    "Invalid unit" instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/temperature",
        json={"value": 21, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 21.0


async def test_temperature_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/temperature",
        json={"value": 1, "from_unit": "rankine", "to_unit": "celsius"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_temperature_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/temperature",
        json={"value": 1, "from_unit": "celsius", "to_unit": "rankine"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_temperature_missing_value_field_rejected_with_422(client, api_key):
    """`TemperatureConvertRequest.value` has no default, so omitting it
    entirely fails Pydantic validation before the handler body ever runs -
    a 422, distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/temperature",
        json={"from_unit": "celsius", "to_unit": "fahrenheit"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_temperature_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/temperature",
        json={"value": 1, "from_unit": "celsius", "to_unit": "fahrenheit"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_temperature_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/temperature",
        json={"value": 1, "from_unit": "celsius", "to_unit": "fahrenheit"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # -10 Kelvin is below absolute zero (0K = -273.15C) and is
        # physically impossible, but `convert_temperature` only validates
        # `from_unit`/`to_unit` names, not the numeric range - it has no
        # concept of absolute zero. This documents that intentional
        # non-validation (see PR discussion) rather than asserting a 400
        # that the handler was never written to raise: celsius = -10 -
        # 273.15 = -283.15 exactly, per the same from-Celsius-base formula
        # as every other case above.
        pytest.param(
            -10, "kelvin", "celsius", -283.15, id="below_absolute_zero_kelvin_to_celsius"
        ),
        # Same negative-Kelvin input converted straight through to
        # Fahrenheit: celsius = -283.15, then *9/5 + 32 = -477.67 exactly.
        pytest.param(
            -10, "kelvin", "fahrenheit", -477.67, id="below_absolute_zero_kelvin_to_fahrenheit"
        ),
        # A very large magnitude value exercises the formula outside the
        # "reasonable temperature" range with no overflow/precision
        # surprises: 1_000_000C -> K is exact float addition,
        # 1_000_000 + 273.15 = 1000273.15.
        pytest.param(
            1_000_000, "celsius", "kelvin", 1000273.15, id="extreme_large_celsius_to_kelvin"
        ),
    ],
)
async def test_temperature_accepts_out_of_physical_range_values_without_400(
    client, api_key, value, from_unit, to_unit, expected
):
    """`convert_temperature` intentionally has no absolute-zero (or any
    other physical-range) validation - only unit *names* are checked
    against `valid_units`. A request with a numerically impossible value
    (e.g. negative Kelvin) still returns 200 with the formula's result,
    not a 400. If range validation is ever added, this test should be
    updated to assert the new rejection behavior instead."""
    resp = await client.post(
        "/v1/unit_converters/temperature",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


async def test_temperature_missing_from_unit_field_rejected_with_422(client, api_key):
    """Complements `test_temperature_missing_value_field_rejected_with_422`
    - `from_unit` is likewise a required field with no default, so omitting
    it also fails Pydantic validation (422) before the handler's own 400
    "Invalid unit" path is ever reached."""
    resp = await client.post(
        "/v1/unit_converters/temperature",
        json={"value": 1, "to_unit": "fahrenheit"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 kilogram = 1000 grams (1 / 0.001 == 1000.0 exactly).
        pytest.param(1, "kilogram", "gram", 1000.0, id="kilogram_to_gram"),
        # 1000 grams = 1 kilogram (inverse of the above).
        pytest.param(1000, "gram", "kilogram", 1.0, id="gram_to_kilogram"),
        # 1 kilogram = 2.2046 pounds (1 / 0.45359237 == 2.204622622...,
        # rounded to 4dp per the router's own `round(result, 4)`).
        pytest.param(1, "kilogram", "pound", 2.2046, id="kilogram_to_pound_rounded"),
        # 1 pound = 0.4536 kilograms (0.45359237 / 1.0 == 0.45359237,
        # rounded to 4dp).
        pytest.param(1, "pound", "kilogram", 0.4536, id="pound_to_kilogram_rounded"),
        # 1 kilogram = 35.274 ounces (1 / 0.028349523125 == 35.27396...,
        # rounded to 4dp).
        pytest.param(1, "kilogram", "ounce", 35.274, id="kilogram_to_ounce_rounded"),
        # 1 ounce = 0.0283 kilograms (0.028349523125 / 1.0 == 0.028349523125,
        # rounded to 4dp).
        pytest.param(1, "ounce", "kilogram", 0.0283, id="ounce_to_kilogram_rounded"),
        # 1 pound = 16 ounces exactly (0.45359237 / 0.028349523125 == 16.0
        # exactly, by definition of these two router constants).
        pytest.param(1, "pound", "ounce", 16.0, id="pound_to_ounce"),
        # 16 ounces = 1 pound (inverse of the above).
        pytest.param(16, "ounce", "pound", 1.0, id="ounce_to_pound"),
        # 500 grams = 1.1023 pounds (500 * 0.001 / 0.45359237 ==
        # 1.10231131..., rounded to 4dp).
        pytest.param(500, "gram", "pound", 1.1023, id="gram_to_pound_rounded"),
        # 5 pounds = 2267.9619 grams (5 * 0.45359237 / 0.001 == 2267.96185,
        # rounded to 4dp).
        pytest.param(5, "pound", "gram", 2267.9619, id="pound_to_gram_rounded"),
    ],
)
async def test_weight_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize("unit", ["kilogram", "gram", "pound", "ounce"])
async def test_all_four_weight_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    weight units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_weight_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": 1, "from_unit": "tonne", "to_unit": "kilogram"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_weight_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": 1, "from_unit": "kilogram", "to_unit": "stone"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_weight_missing_value_field_rejected_with_422(client, api_key):
    """`WeightConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"from_unit": "kilogram", "to_unit": "pound"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_weight_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": 1, "from_unit": "kilogram", "to_unit": "pound"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_weight_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": 1, "from_unit": "kilogram", "to_unit": "pound"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_weight_missing_to_unit_field_rejected_with_422(client, api_key):
    """Complements `test_weight_missing_value_field_rejected_with_422` -
    `to_unit` is likewise a required field with no default on
    `WeightConvertRequest`, so omitting it also fails Pydantic validation
    (422) before the handler's own 400 "Invalid unit" path is ever reached.
    Neither the length nor temperature suites in this file cover this field
    either - filed as a gap, closed here first."""
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": 1, "from_unit": "kilogram"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_weight_missing_from_unit_field_rejected_with_422(client, api_key):
    """Mirrors
    `test_temperature_missing_from_unit_field_rejected_with_422` - `from_unit`
    is a required field with no default, so omitting it also fails Pydantic
    validation (422) before the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": 1, "to_unit": "pound"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_weight_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `WeightConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "kilogram", "to_unit": "pound"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/weight",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # Zero converts to zero regardless of unit pair - both units
        # multiply/divide a zero value, so the result is exactly 0.0 either
        # way.
        pytest.param(0, "kilogram", "pound", 0.0, id="zero_kilogram_to_pound"),
        pytest.param(0, "pound", "kilogram", 0.0, id="zero_pound_to_kilogram"),
    ],
)
async def test_weight_zero_value_converts_to_zero(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == expected


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # `convert_weight` has no non-negative validation - a negative
        # value (physically meaningless for a mass) still runs through the
        # same `value * units[from_unit] / units[to_unit]` formula and
        # returns 200, not a 400. This documents that intentional
        # non-validation rather than asserting a rejection the handler was
        # never written to raise (mirrors
        # `test_temperature_accepts_out_of_physical_range_values_without_400`).
        # -5 * 1.0 / 0.001 == -5000.0 exactly.
        pytest.param(-5, "kilogram", "gram", -5000.0, id="negative_kilogram_to_gram"),
        # -1 * 0.45359237 / 0.028349523125 == -16.0 exactly (same clean
        # 16-ounces-per-pound identity as `pound_to_ounce` above, negated).
        pytest.param(-1, "pound", "ounce", -16.0, id="negative_pound_to_ounce"),
    ],
)
async def test_weight_accepts_negative_values_without_400(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/weight",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


async def test_weight_round_trip_kilogram_to_pound_to_kilogram_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover (approximately)
    the original value. Independently verified: 10 / 0.45359237 ==
    22.046226218487758, which the router rounds to 4dp (22.0462); converting
    22.0462 back (22.0462 * 0.45359237 == 10.00000029038794, rounded to
    10.0) recovers the exact original here - the rounding error at 4dp
    happens to cancel out for this value, but the assertion below uses a
    tolerance rather than depending on that coincidence."""
    first = await client.post(
        "/v1/unit_converters/weight",
        json={"value": 10, "from_unit": "kilogram", "to_unit": "pound"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 22.0462

    second = await client.post(
        "/v1/unit_converters/weight",
        json={"value": intermediate, "from_unit": "pound", "to_unit": "kilogram"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-3)


async def test_weight_round_trip_gram_to_ounce_to_gram_returns_approximately_original(
    client, api_key
):
    """Same round-trip shape as the kilogram/pound test above, but with a
    unit pair where the two roundings do NOT cancel out exactly - 250g ->
    ounce -> g independently computes to 250.0003, not 250.0, demonstrating
    why round-trip assertions need a tolerance rather than exact equality in
    general."""
    first = await client.post(
        "/v1/unit_converters/weight",
        json={"value": 250, "from_unit": "gram", "to_unit": "ounce"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 8.8185

    second = await client.post(
        "/v1/unit_converters/weight",
        json={"value": intermediate, "from_unit": "ounce", "to_unit": "gram"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(250.0, abs=1e-2)
    assert final != 250.0


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 4046.8564224 square meters = 1 acre (exact, by definition of the
        # router's own `acre` constant).
        pytest.param(
            4046.8564224, "square_meter", "acre", 1.0, id="square_meter_to_acre"
        ),
        # 1 acre = 4046.8564 square meters (4046.8564224 rounded to 4dp).
        pytest.param(1, "acre", "square_meter", 4046.8564, id="acre_to_square_meter"),
        # 9 square feet = 1 square yard (a yard is 3 feet on a side, so
        # 3^2 == 9 exactly: 0.83612736 / 0.09290304 == 9.0 exactly).
        pytest.param(9, "square_foot", "square_yard", 1.0, id="square_foot_to_square_yard"),
        # 1 square yard = 9 square feet (inverse of the above).
        pytest.param(1, "square_yard", "square_foot", 9.0, id="square_yard_to_square_foot"),
        # 5 square kilometers = 5,000,000 square meters (1,000,000 * 5
        # exactly).
        pytest.param(
            5, "square_kilometer", "square_meter", 5_000_000.0, id="square_km_to_square_meter"
        ),
        # 2,500,000 square meters = 2.5 square kilometers (inverse of the
        # above).
        pytest.param(
            2_500_000, "square_meter", "square_kilometer", 2.5, id="square_meter_to_square_km"
        ),
        # 10 square meters = 107.6391 square feet (10 / 0.09290304 ==
        # 107.63910416..., rounded to 4dp).
        pytest.param(
            10, "square_meter", "square_foot", 107.6391, id="square_meter_to_square_foot_rounded"
        ),
        # 1 acre = 43,560 square feet, so 43,560 square feet = 1 acre exactly
        # (a well-known real-world identity, also exact from the router's
        # own constants: 43560 * 0.09290304 / 4046.8564224 == 1.0).
        pytest.param(43_560, "square_foot", "acre", 1.0, id="square_foot_to_acre"),
        # 2 acres = 87,120 square feet (inverse of the above, doubled).
        pytest.param(2, "acre", "square_foot", 87_120.0, id="acre_to_square_foot"),
        # 1 square kilometer = 247.1054 acres (1,000,000 / 4046.8564224 ==
        # 247.10538..., rounded to 4dp).
        pytest.param(1, "square_kilometer", "acre", 247.1054, id="square_kilometer_to_acre"),
    ],
)
async def test_area_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/area",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["square_meter", "square_kilometer", "square_foot", "square_yard", "acre"],
)
async def test_all_five_area_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 5
    area units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/area",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_area_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/area",
        json={"value": 1, "from_unit": "hectare", "to_unit": "square_meter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_area_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/area",
        json={"value": 1, "from_unit": "square_meter", "to_unit": "hectare"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_area_missing_value_field_rejected_with_422(client, api_key):
    """`AreaConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/area",
        json={"from_unit": "square_meter", "to_unit": "square_foot"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_area_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/area",
        json={"value": 1, "to_unit": "square_foot"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_area_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/area",
        json={"value": 1, "from_unit": "square_meter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_area_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/area",
        json={"value": 1, "from_unit": "square_meter", "to_unit": "square_foot"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_area_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/area",
        json={"value": 1, "from_unit": "square_meter", "to_unit": "square_foot"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_area_round_trip_square_meter_to_square_foot_to_square_meter_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover the original
    value. Independently verified: 10 / 0.09290304 == 107.63910416...,
    which the router rounds to 4dp (107.6391); converting 107.6391 back
    (107.6391 * 0.09290304 == 9.99999977..., rounded to 10.0) recovers the
    exact original here - the assertion below still uses a tolerance rather
    than depending on that coincidence, mirroring the weight round-trip
    tests above."""
    first = await client.post(
        "/v1/unit_converters/area",
        json={"value": 10, "from_unit": "square_meter", "to_unit": "square_foot"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 107.6391

    second = await client.post(
        "/v1/unit_converters/area",
        json={"value": intermediate, "from_unit": "square_foot", "to_unit": "square_meter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-3)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 liter = 1000 milliliters (1 / 0.001 == 1000.0 exactly).
        pytest.param(1, "liter", "milliliter", 1000.0, id="liter_to_milliliter"),
        # 1000 milliliters = 1 liter (inverse of the above).
        pytest.param(1000, "milliliter", "liter", 1.0, id="milliliter_to_liter"),
        # 1 cubic meter = 1000 liters (exact, by definition of the router's
        # own `cubic_meter` constant).
        pytest.param(1, "cubic_meter", "liter", 1000.0, id="cubic_meter_to_liter"),
        # 2500 liters = 2.5 cubic meters (inverse ratio of the above).
        pytest.param(2500, "liter", "cubic_meter", 2.5, id="liter_to_cubic_meter"),
        # 1 gallon = 3.7854 liters (3.785411784 / 1.0, rounded to 4dp per
        # the router's own `round(result, 4)`).
        pytest.param(1, "gallon", "liter", 3.7854, id="gallon_to_liter_rounded"),
        # 10 liters = 2.6417 gallons (10 / 3.785411784 == 2.64172052...,
        # rounded to 4dp).
        pytest.param(10, "liter", "gallon", 2.6417, id="liter_to_gallon_rounded"),
        # 1 cubic foot = 28.3168 liters (28.316846592 / 1.0, rounded to
        # 4dp).
        pytest.param(1, "cubic_foot", "liter", 28.3168, id="cubic_foot_to_liter_rounded"),
        # 100 liters = 3.5315 cubic feet (100 / 28.316846592 ==
        # 3.53146667..., rounded to 4dp).
        pytest.param(100, "liter", "cubic_foot", 3.5315, id="liter_to_cubic_foot_rounded"),
        # 1 gallon = 0.1337 cubic feet (3.785411784 / 28.316846592 ==
        # 0.13368055..., rounded to 4dp).
        pytest.param(1, "gallon", "cubic_foot", 0.1337, id="gallon_to_cubic_foot_rounded"),
        # 1 cubic foot = 7.4805 gallons (28.316846592 / 3.785411784 ==
        # 7.48051948..., rounded to 4dp).
        pytest.param(1, "cubic_foot", "gallon", 7.4805, id="cubic_foot_to_gallon_rounded"),
        # 1 cubic meter = 264.1721 gallons (1000.0 / 3.785411784 ==
        # 264.17205235..., rounded to 4dp).
        pytest.param(1, "cubic_meter", "gallon", 264.1721, id="cubic_meter_to_gallon_rounded"),
    ],
)
async def test_volume_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["liter", "milliliter", "cubic_meter", "gallon", "cubic_foot"],
)
async def test_all_five_volume_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 5
    volume units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_volume_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": 1, "from_unit": "pint", "to_unit": "liter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_volume_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": 1, "from_unit": "liter", "to_unit": "pint"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_volume_missing_value_field_rejected_with_422(client, api_key):
    """`VolumeConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"from_unit": "liter", "to_unit": "gallon"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_volume_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": 1, "to_unit": "gallon"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_volume_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": 1, "from_unit": "liter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_volume_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": 1, "from_unit": "liter", "to_unit": "gallon"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_volume_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": 1, "from_unit": "liter", "to_unit": "gallon"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_volume_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `VolumeConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "liter", "to_unit": "gallon"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/volume",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # Zero converts to zero regardless of unit pair.
        pytest.param(0, "liter", "gallon", 0.0, id="zero_liter_to_gallon"),
        pytest.param(0, "gallon", "liter", 0.0, id="zero_gallon_to_liter"),
    ],
)
async def test_volume_zero_value_converts_to_zero(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == expected


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # `convert_volume` has no non-negative validation - a negative value
        # (physically meaningless for a volume) still runs through the same
        # `value * units[from_unit] / units[to_unit]` formula and returns
        # 200, not a 400. This documents that intentional non-validation
        # rather than asserting a rejection the handler was never written to
        # raise (mirrors
        # `test_weight_accepts_negative_values_without_400`, a gap this
        # volume suite was otherwise missing relative to its weight
        # sibling).
        # -5 * 1.0 / 3.785411784 == -1.32086..., rounded to 4dp == -1.3209.
        pytest.param(-5, "liter", "gallon", -1.3209, id="negative_liter_to_gallon"),
        # -1 * 28.316846592 / 1.0 == -28.316846592 exactly, rounded to 4dp
        # == -28.3168.
        pytest.param(-1, "cubic_foot", "liter", -28.3168, id="negative_cubic_foot_to_liter"),
    ],
)
async def test_volume_accepts_negative_values_without_400(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/volume",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


async def test_volume_round_trip_liter_to_gallon_to_liter_returns_approximately_original(
    client, api_key
):
    """A round trip through two conversions should recover (approximately)
    the original value. Independently verified: 10 / 3.785411784 ==
    2.64172052..., which the router rounds to 4dp (2.6417); converting
    2.6417 back (2.6417 * 3.785411784 == 9.99990..., rounded to 9.9999)
    does NOT recover the exact original at 4dp precision - the assertion
    below uses a tolerance rather than exact equality, mirroring the area
    and weight round-trip tests above."""
    first = await client.post(
        "/v1/unit_converters/volume",
        json={"value": 10, "from_unit": "liter", "to_unit": "gallon"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 2.6417

    second = await client.post(
        "/v1/unit_converters/volume",
        json={"value": intermediate, "from_unit": "gallon", "to_unit": "liter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-3)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 kilometer_per_hour = 0.2778 meter_per_second (0.277777778 / 1.0,
        # rounded to 4dp per the router's own `round(result, 4)`).
        pytest.param(
            1, "kilometer_per_hour", "meter_per_second", 0.2778, id="kmh_to_mps"
        ),
        # 1 mile_per_hour = 0.447 meter_per_second (0.44704 / 1.0, rounded to
        # 4dp).
        pytest.param(1, "mile_per_hour", "meter_per_second", 0.447, id="mph_to_mps"),
        # 1 knot = 0.5144 meter_per_second (0.514444444 / 1.0, rounded to
        # 4dp).
        pytest.param(1, "knot", "meter_per_second", 0.5144, id="knot_to_mps"),
        # 1 mile_per_hour = 0.869 knots (0.44704 / 0.514444444 ==
        # 0.868976..., rounded to 4dp).
        pytest.param(1, "mile_per_hour", "knot", 0.869, id="mph_to_knot"),
        # 100 kilometer_per_hour = 62.1371 mile_per_hour (100 * 0.277777778 /
        # 0.44704 == 62.13711922..., rounded to 4dp).
        pytest.param(
            100, "kilometer_per_hour", "mile_per_hour", 62.1371, id="kmh_to_mph"
        ),
    ],
)
async def test_speed_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/speed",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["meter_per_second", "kilometer_per_hour", "mile_per_hour", "knot"],
)
async def test_all_four_speed_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    speed units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/speed",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_speed_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/speed",
        json={"value": 1, "from_unit": "mach", "to_unit": "meter_per_second"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_speed_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/speed",
        json={"value": 1, "from_unit": "meter_per_second", "to_unit": "mach"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_speed_missing_value_field_rejected_with_422(client, api_key):
    """`SpeedConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/speed",
        json={"from_unit": "meter_per_second", "to_unit": "knot"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_speed_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/speed",
        json={"value": 1, "to_unit": "knot"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_speed_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/speed",
        json={"value": 1, "from_unit": "meter_per_second"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_speed_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/speed",
        json={"value": 1, "from_unit": "meter_per_second", "to_unit": "knot"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_speed_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/speed",
        json={"value": 1, "from_unit": "meter_per_second", "to_unit": "knot"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_speed_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `SpeedConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "meter_per_second", "to_unit": "knot"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/speed",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_speed_round_trip_kmh_to_mph_to_kmh_returns_approximately_original(
    client, api_key
):
    """A round trip through two conversions should recover (approximately)
    the original value. Independently verified: 100 * 0.277777778 /
    0.44704 == 62.13711922..., which the router rounds to 4dp (62.1371);
    converting 62.1371 back (62.1371 * 0.44704 / 0.277777778 ==
    99.99999...) recovers the original within a small tolerance."""
    first = await client.post(
        "/v1/unit_converters/speed",
        json={"value": 100, "from_unit": "kilometer_per_hour", "to_unit": "mile_per_hour"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 62.1371

    second = await client.post(
        "/v1/unit_converters/speed",
        json={"value": intermediate, "from_unit": "mile_per_hour", "to_unit": "kilometer_per_hour"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(100.0, abs=1e-3)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 hour = 60 minutes (3600.0 / 60.0 == 60.0 exactly).
        pytest.param(1, "hour", "minute", 60.0, id="hour_to_minute"),
        # 1 day = 24 hours (86400.0 / 3600.0 == 24.0 exactly).
        pytest.param(1, "day", "hour", 24.0, id="day_to_hour"),
        # 1 week = 7 days (604800.0 / 86400.0 == 7.0 exactly).
        pytest.param(1, "week", "day", 7.0, id="week_to_day"),
        # 1 week = 604800 seconds (exact, by definition of the router's own
        # `week` constant).
        pytest.param(1, "week", "second", 604800.0, id="week_to_second"),
        # 3600 seconds = 1 hour (inverse of the base `hour` constant).
        pytest.param(3600, "second", "hour", 1.0, id="second_to_hour"),
    ],
)
async def test_time_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/time",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["second", "minute", "hour", "day", "week"],
)
async def test_all_five_time_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 5
    time units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/time",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_time_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/time",
        json={"value": 1, "from_unit": "fortnight", "to_unit": "day"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_time_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/time",
        json={"value": 1, "from_unit": "day", "to_unit": "fortnight"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_time_missing_value_field_rejected_with_422(client, api_key):
    """`TimeConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/time",
        json={"from_unit": "hour", "to_unit": "minute"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_time_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/time",
        json={"value": 1, "to_unit": "minute"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_time_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/time",
        json={"value": 1, "from_unit": "hour"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_time_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/time",
        json={"value": 1, "from_unit": "hour", "to_unit": "minute"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_time_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/time",
        json={"value": 1, "from_unit": "hour", "to_unit": "minute"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_time_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `TimeConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "hour", "to_unit": "minute"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/time",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_time_round_trip_day_to_hour_to_day_returns_original(client, api_key):
    """A round trip through two conversions should recover the original
    value exactly here: 5 * 86400.0 / 3600.0 == 120.0 exactly (no rounding
    error introduced, unlike the affine/irrational-ratio round trips in the
    weight/area/volume suites above), and 120.0 * 3600.0 / 86400.0 == 5.0
    exactly."""
    first = await client.post(
        "/v1/unit_converters/time",
        json={"value": 5, "from_unit": "day", "to_unit": "hour"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 120.0

    second = await client.post(
        "/v1/unit_converters/time",
        json={"value": intermediate, "from_unit": "hour", "to_unit": "day"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(5.0, abs=1e-9)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 kilobyte = 1024 bytes (exact, by definition of the router's own
        # binary `kilobyte` constant).
        pytest.param(1, "kilobyte", "byte", 1024.0, id="kilobyte_to_byte"),
        # 1024 bytes = 1 kilobyte (inverse of the above).
        pytest.param(1024, "byte", "kilobyte", 1.0, id="byte_to_kilobyte"),
        # 1 megabyte = 1024 kilobytes (1024.0**2 / 1024.0 == 1024.0 exactly).
        pytest.param(1, "megabyte", "kilobyte", 1024.0, id="megabyte_to_kilobyte"),
        # 1 gigabyte = 1024 megabytes (1024.0**3 / 1024.0**2 == 1024.0
        # exactly).
        pytest.param(1, "gigabyte", "megabyte", 1024.0, id="gigabyte_to_megabyte"),
        # 1 terabyte = 1024 gigabytes (1024.0**4 / 1024.0**3 == 1024.0
        # exactly).
        pytest.param(1, "terabyte", "gigabyte", 1024.0, id="terabyte_to_gigabyte"),
    ],
)
async def test_data_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/data",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["byte", "kilobyte", "megabyte", "gigabyte", "terabyte"],
)
async def test_all_five_data_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 5
    data-storage units as both a valid `from_unit` and a valid `to_unit`
    cheaply: any unit not in the router's `units` dict would 400 with
    "Invalid unit" instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/data",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_data_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/data",
        json={"value": 1, "from_unit": "bit", "to_unit": "byte"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_data_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/data",
        json={"value": 1, "from_unit": "byte", "to_unit": "bit"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_data_missing_value_field_rejected_with_422(client, api_key):
    """`DataConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/data",
        json={"from_unit": "megabyte", "to_unit": "kilobyte"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_data_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/data",
        json={"value": 1, "to_unit": "kilobyte"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_data_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/data",
        json={"value": 1, "from_unit": "megabyte"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_data_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/data",
        json={"value": 1, "from_unit": "megabyte", "to_unit": "kilobyte"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_data_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/data",
        json={"value": 1, "from_unit": "megabyte", "to_unit": "kilobyte"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_data_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `DataConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "megabyte", "to_unit": "kilobyte"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/data",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_data_round_trip_gigabyte_to_megabyte_to_gigabyte_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover the original
    value exactly here: 5 * 1024.0**3 / 1024.0**2 == 5120.0 exactly (clean
    binary-multiple ratio, no rounding error), and 5120.0 * 1024.0**2 /
    1024.0**3 == 5.0 exactly."""
    first = await client.post(
        "/v1/unit_converters/data",
        json={"value": 5, "from_unit": "gigabyte", "to_unit": "megabyte"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 5120.0

    second = await client.post(
        "/v1/unit_converters/data",
        json={"value": intermediate, "from_unit": "megabyte", "to_unit": "gigabyte"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(5.0, abs=1e-9)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 kilojoule = 1000 joules (exact, by definition of the router's
        # own `kilojoule` constant).
        pytest.param(1, "kilojoule", "joule", 1000.0, id="kilojoule_to_joule"),
        # 1 kilocalorie = 4.184 kilojoules (4184.0 / 1000.0 == 4.184
        # exactly).
        pytest.param(1, "kilocalorie", "kilojoule", 4.184, id="kilocalorie_to_kilojoule"),
        # 1 calorie = 4.184 joules (exact, by definition of the router's own
        # `calorie` constant).
        pytest.param(1, "calorie", "joule", 4.184, id="calorie_to_joule"),
        # 1 watt_hour = 3600 joules (exact, by definition of the router's
        # own `watt_hour` constant).
        pytest.param(1, "watt_hour", "joule", 3600.0, id="watt_hour_to_joule"),
        # 1 kilocalorie = 1000 calories (4184.0 / 4.184 == 1000.0 exactly).
        pytest.param(1, "kilocalorie", "calorie", 1000.0, id="kilocalorie_to_calorie"),
    ],
)
async def test_energy_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/energy",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["joule", "kilojoule", "calorie", "kilocalorie", "watt_hour"],
)
async def test_all_five_energy_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 5
    energy units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/energy",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_energy_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/energy",
        json={"value": 1, "from_unit": "btu", "to_unit": "joule"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_energy_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/energy",
        json={"value": 1, "from_unit": "joule", "to_unit": "btu"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_energy_missing_value_field_rejected_with_422(client, api_key):
    """`EnergyConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/energy",
        json={"from_unit": "kilocalorie", "to_unit": "kilojoule"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_energy_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/energy",
        json={"value": 1, "to_unit": "kilojoule"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_energy_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/energy",
        json={"value": 1, "from_unit": "kilocalorie"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_energy_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/energy",
        json={"value": 1, "from_unit": "kilocalorie", "to_unit": "kilojoule"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_energy_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/energy",
        json={"value": 1, "from_unit": "kilocalorie", "to_unit": "kilojoule"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_energy_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `EnergyConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "kilocalorie", "to_unit": "kilojoule"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/energy",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_energy_round_trip_kilocalorie_to_joule_to_kilocalorie_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover the original
    value exactly here: 10 * 4184.0 / 1.0 == 41840.0 exactly, and 41840.0 *
    1.0 / 4184.0 == 10.0 exactly (clean ratio, no rounding error)."""
    first = await client.post(
        "/v1/unit_converters/energy",
        json={"value": 10, "from_unit": "kilocalorie", "to_unit": "joule"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 41840.0

    second = await client.post(
        "/v1/unit_converters/energy",
        json={"value": intermediate, "from_unit": "joule", "to_unit": "kilocalorie"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-9)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 kilowatt = 1000 watts (exact, by definition of the router's own
        # `kilowatt` constant).
        pytest.param(1, "kilowatt", "watt", 1000.0, id="kilowatt_to_watt"),
        # 1 horsepower = 745.6999 watts (745.699872 rounded to 4dp).
        pytest.param(1, "horsepower", "watt", 745.6999, id="horsepower_to_watt_rounded"),
        # 1 megawatt = 1,000,000 watts (exact, by definition of the router's
        # own `megawatt` constant).
        pytest.param(1, "megawatt", "watt", 1_000_000.0, id="megawatt_to_watt"),
        # 1 kilowatt = 1.341 horsepower (1000.0 / 745.699872 ==
        # 1.34102209..., rounded to 4dp).
        pytest.param(1, "kilowatt", "horsepower", 1.341, id="kilowatt_to_horsepower_rounded"),
        # 10 horsepower = 7.457 kilowatts (10 * 745.699872 / 1000.0 ==
        # 7.45699872, rounded to 4dp).
        pytest.param(10, "horsepower", "kilowatt", 7.457, id="horsepower_to_kilowatt_rounded"),
    ],
)
async def test_power_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/power",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["watt", "kilowatt", "horsepower", "megawatt"],
)
async def test_all_four_power_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    power units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/power",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_power_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/power",
        json={"value": 1, "from_unit": "btu_per_hour", "to_unit": "watt"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_power_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/power",
        json={"value": 1, "from_unit": "watt", "to_unit": "btu_per_hour"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_power_missing_value_field_rejected_with_422(client, api_key):
    """`PowerConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/power",
        json={"from_unit": "kilowatt", "to_unit": "horsepower"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_power_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/power",
        json={"value": 1, "to_unit": "horsepower"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_power_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/power",
        json={"value": 1, "from_unit": "kilowatt"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_power_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/power",
        json={"value": 1, "from_unit": "kilowatt", "to_unit": "horsepower"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_power_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/power",
        json={"value": 1, "from_unit": "kilowatt", "to_unit": "horsepower"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_power_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `PowerConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "kilowatt", "to_unit": "horsepower"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/power",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_power_round_trip_kilowatt_to_horsepower_to_kilowatt_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover (approximately)
    the original value. Independently verified: 10 * 1000.0 / 745.699872 ==
    13.41021806..., which the router rounds to 4dp (13.4102); converting
    13.4102 back (13.4102 * 745.699872 / 1000.0 == 9.99992...) recovers the
    original within a small tolerance."""
    first = await client.post(
        "/v1/unit_converters/power",
        json={"value": 10, "from_unit": "kilowatt", "to_unit": "horsepower"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 13.4102

    second = await client.post(
        "/v1/unit_converters/power",
        json={"value": intermediate, "from_unit": "horsepower", "to_unit": "kilowatt"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-3)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 atmosphere = 101325 pascals (exact, by definition of the
        # router's own `atmosphere` constant).
        pytest.param(1, "atmosphere", "pascal", 101325.0, id="atmosphere_to_pascal"),
        # 1 bar = 100000 pascals (exact, by definition of the router's own
        # `bar` constant).
        pytest.param(1, "bar", "pascal", 100000.0, id="bar_to_pascal"),
        # 1 psi = 6894.7573 pascals (6894.757293168 rounded to 4dp).
        pytest.param(1, "psi", "pascal", 6894.7573, id="psi_to_pascal_rounded"),
        # 1 bar = 14.5038 psi (100000.0 / 6894.757293168 == 14.50377377...,
        # rounded to 4dp).
        pytest.param(1, "bar", "psi", 14.5038, id="bar_to_psi_rounded"),
        # 1 atmosphere = 1.0132 bar (101325.0 / 100000.0 == 1.01325, rounded
        # to 4dp).
        pytest.param(1, "atmosphere", "bar", 1.0132, id="atmosphere_to_bar_rounded"),
    ],
)
async def test_pressure_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["pascal", "atmosphere", "bar", "psi"],
)
async def test_all_four_pressure_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    pressure units as both a valid `from_unit` and a valid `to_unit`
    cheaply: any unit not in the router's `units` dict would 400 with
    "Invalid unit" instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_pressure_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": 1, "from_unit": "torr", "to_unit": "pascal"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_pressure_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": 1, "from_unit": "pascal", "to_unit": "torr"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_pressure_missing_value_field_rejected_with_422(client, api_key):
    """`PressureConvertRequest.value` has no default, so omitting it
    entirely fails Pydantic validation before the handler body ever runs -
    a 422, distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json={"from_unit": "bar", "to_unit": "psi"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_pressure_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": 1, "to_unit": "psi"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_pressure_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": 1, "from_unit": "bar"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_pressure_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": 1, "from_unit": "bar", "to_unit": "psi"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_pressure_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": 1, "from_unit": "bar", "to_unit": "psi"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_pressure_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `PressureConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "bar", "to_unit": "psi"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/pressure",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_pressure_round_trip_bar_to_psi_to_bar_returns_original(client, api_key):
    """A round trip through two conversions should recover (approximately)
    the original value. Independently verified: 10 * 100000.0 /
    6894.757293168 == 145.03773772..., which the router rounds to 4dp
    (145.0377); converting 145.0377 back (145.0377 * 6894.757293168 /
    100000.0 == 9.99998...) recovers the original within a small
    tolerance."""
    first = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": 10, "from_unit": "bar", "to_unit": "psi"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 145.0377

    second = await client.post(
        "/v1/unit_converters/pressure",
        json={"value": intermediate, "from_unit": "psi", "to_unit": "bar"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-3)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 kilohertz = 1000 hertz (exact, by definition of the router's
        # own `kilohertz` constant).
        pytest.param(1, "kilohertz", "hertz", 1000.0, id="kilohertz_to_hertz"),
        # 1 megahertz = 1000 kilohertz (1e6 / 1e3 == 1000.0 exactly).
        pytest.param(1, "megahertz", "kilohertz", 1000.0, id="megahertz_to_kilohertz"),
        # 1 gigahertz = 1000 megahertz (1e9 / 1e6 == 1000.0 exactly).
        pytest.param(1, "gigahertz", "megahertz", 1000.0, id="gigahertz_to_megahertz"),
        # 1 gigahertz = 1,000,000,000 hertz (exact, by definition of the
        # router's own `gigahertz` constant).
        pytest.param(1, "gigahertz", "hertz", 1_000_000_000.0, id="gigahertz_to_hertz"),
    ],
)
async def test_frequency_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["hertz", "kilohertz", "megahertz", "gigahertz"],
)
async def test_all_four_frequency_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    frequency units as both a valid `from_unit` and a valid `to_unit`
    cheaply: any unit not in the router's `units` dict would 400 with
    "Invalid unit" instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_frequency_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": 1, "from_unit": "terahertz", "to_unit": "hertz"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_frequency_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": 1, "from_unit": "hertz", "to_unit": "terahertz"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_frequency_missing_value_field_rejected_with_422(client, api_key):
    """`FrequencyConvertRequest.value` has no default, so omitting it
    entirely fails Pydantic validation before the handler body ever runs -
    a 422, distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json={"from_unit": "megahertz", "to_unit": "kilohertz"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_frequency_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": 1, "to_unit": "kilohertz"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_frequency_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": 1, "from_unit": "megahertz"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_frequency_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": 1, "from_unit": "megahertz", "to_unit": "kilohertz"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_frequency_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": 1, "from_unit": "megahertz", "to_unit": "kilohertz"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_frequency_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `FrequencyConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "megahertz", "to_unit": "kilohertz"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/frequency",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_frequency_round_trip_gigahertz_to_megahertz_to_gigahertz_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover the original
    value exactly here: 10 * 1e9 / 1e6 == 10000.0 exactly (clean
    power-of-ten ratio, no rounding error), and 10000.0 * 1e6 / 1e9 == 10.0
    exactly."""
    first = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": 10, "from_unit": "gigahertz", "to_unit": "megahertz"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 10000.0

    second = await client.post(
        "/v1/unit_converters/frequency",
        json={"value": intermediate, "from_unit": "megahertz", "to_unit": "gigahertz"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-9)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 kilonewton = 1000 newtons (exact, by definition of the router's
        # own `kilonewton` constant).
        pytest.param(1, "kilonewton", "newton", 1000.0, id="kilonewton_to_newton"),
        # 1 pound-force = 4.4482 newtons (4.4482216152605 rounded to 4dp).
        pytest.param(1, "pound_force", "newton", 4.4482, id="pound_force_to_newton_rounded"),
        # 1 kilogram-force = 9.8066 newtons (9.80665 rounded to 4dp).
        pytest.param(
            1, "kilogram_force", "newton", 9.8066, id="kilogram_force_to_newton_rounded"
        ),
        # 1 kilogram-force = 2.2046 pound-force (9.80665 / 4.4482216152605
        # == 2.20462262..., rounded to 4dp).
        pytest.param(
            1, "kilogram_force", "pound_force", 2.2046, id="kilogram_force_to_pound_force_rounded"
        ),
        # 10 newtons = 2.2481 pound-force (10 / 4.4482216152605 ==
        # 2.24808943..., rounded to 4dp).
        pytest.param(10, "newton", "pound_force", 2.2481, id="newton_to_pound_force_rounded"),
    ],
)
async def test_force_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/force",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["newton", "kilonewton", "pound_force", "kilogram_force"],
)
async def test_all_four_force_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    force units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/force",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_force_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/force",
        json={"value": 1, "from_unit": "dyne", "to_unit": "newton"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_force_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/force",
        json={"value": 1, "from_unit": "newton", "to_unit": "dyne"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_force_missing_value_field_rejected_with_422(client, api_key):
    """`ForceConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/force",
        json={"from_unit": "kilogram_force", "to_unit": "pound_force"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_force_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/force",
        json={"value": 1, "to_unit": "pound_force"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_force_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/force",
        json={"value": 1, "from_unit": "kilogram_force"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_force_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/force",
        json={"value": 1, "from_unit": "kilogram_force", "to_unit": "pound_force"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_force_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/force",
        json={"value": 1, "from_unit": "kilogram_force", "to_unit": "pound_force"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_force_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `ForceConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "kilogram_force", "to_unit": "pound_force"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/force",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_force_round_trip_kilogram_force_to_pound_force_to_kilogram_force_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover (approximately)
    the original value. Independently verified: 10 * 9.80665 /
    4.4482216152605 == 22.04622622..., which the router rounds to 4dp
    (22.0462); converting 22.0462 back (22.0462 * 4.4482216152605 /
    9.80665 == 9.99999...) recovers the original within a small
    tolerance."""
    first = await client.post(
        "/v1/unit_converters/force",
        json={"value": 10, "from_unit": "kilogram_force", "to_unit": "pound_force"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 22.0462

    second = await client.post(
        "/v1/unit_converters/force",
        json={"value": intermediate, "from_unit": "pound_force", "to_unit": "kilogram_force"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-3)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 foot-pound = 1.3558 newton-meters (1.3558179483314 rounded to
        # 4dp).
        pytest.param(1, "foot_pound", "newton_meter", 1.3558, id="foot_pound_to_newton_meter_rounded"),
        # 1 inch-pound = 0.113 newton-meters (0.1129848290276 rounded to
        # 4dp).
        pytest.param(1, "inch_pound", "newton_meter", 0.113, id="inch_pound_to_newton_meter_rounded"),
        # 1 kilogram-force meter = 9.8066 newton-meters (9.80665 rounded to
        # 4dp).
        pytest.param(
            1,
            "kilogram_force_meter",
            "newton_meter",
            9.8066,
            id="kilogram_force_meter_to_newton_meter_rounded",
        ),
        # 1 foot-pound = 12 inch-pounds exactly (1.3558179483314 /
        # 0.1129848290276 == 12.0 exactly, by definition of these two
        # router constants).
        pytest.param(1, "foot_pound", "inch_pound", 12.0, id="foot_pound_to_inch_pound"),
        # 10 newton-meters = 7.3756 foot-pounds (10 / 1.3558179483314 ==
        # 7.37561669..., rounded to 4dp).
        pytest.param(10, "newton_meter", "foot_pound", 7.3756, id="newton_meter_to_foot_pound_rounded"),
    ],
)
async def test_torque_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/torque",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["newton_meter", "foot_pound", "inch_pound", "kilogram_force_meter"],
)
async def test_all_four_torque_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    torque units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/torque",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_torque_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/torque",
        json={"value": 1, "from_unit": "dyne_centimeter", "to_unit": "newton_meter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_torque_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/torque",
        json={"value": 1, "from_unit": "newton_meter", "to_unit": "dyne_centimeter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_torque_missing_value_field_rejected_with_422(client, api_key):
    """`TorqueConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/torque",
        json={"from_unit": "foot_pound", "to_unit": "inch_pound"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_torque_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/torque",
        json={"value": 1, "to_unit": "inch_pound"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_torque_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/torque",
        json={"value": 1, "from_unit": "foot_pound"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_torque_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/torque",
        json={"value": 1, "from_unit": "foot_pound", "to_unit": "inch_pound"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_torque_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/torque",
        json={"value": 1, "from_unit": "foot_pound", "to_unit": "inch_pound"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_torque_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `TorqueConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "foot_pound", "to_unit": "inch_pound"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/torque",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_torque_round_trip_foot_pound_to_inch_pound_to_foot_pound_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover the original
    value exactly here: 10 * 1.3558179483314 / 0.1129848290276 == 120.0
    exactly (clean 12x ratio, no rounding error), and 120.0 *
    0.1129848290276 / 1.3558179483314 == 10.0 exactly."""
    first = await client.post(
        "/v1/unit_converters/torque",
        json={"value": 10, "from_unit": "foot_pound", "to_unit": "inch_pound"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 120.0

    second = await client.post(
        "/v1/unit_converters/torque",
        json={"value": intermediate, "from_unit": "inch_pound", "to_unit": "foot_pound"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-9)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 g/cm3 = 1000 kg/m3 (exact, by definition of the router's own
        # `g_per_cubic_cm` constant).
        pytest.param(1, "g_per_cubic_cm", "kg_per_cubic_meter", 1000.0, id="g_per_cubic_cm_to_kg_per_cubic_meter"),
        # 1 lb/ft3 = 16.0185 kg/m3 (16.018463374 rounded to 4dp).
        pytest.param(
            1, "lb_per_cubic_ft", "kg_per_cubic_meter", 16.0185, id="lb_per_cubic_ft_to_kg_per_cubic_meter_rounded"
        ),
        # 1 kg/L = 1000 kg/m3 (exact, by definition of the router's own
        # `kg_per_liter` constant).
        pytest.param(1, "kg_per_liter", "kg_per_cubic_meter", 1000.0, id="kg_per_liter_to_kg_per_cubic_meter"),
        # 1 g/cm3 = 1 kg/L (1000.0 / 1000.0 == 1.0 exactly, by definition of
        # these two router constants).
        pytest.param(1, "g_per_cubic_cm", "kg_per_liter", 1.0, id="g_per_cubic_cm_to_kg_per_liter"),
        # 100 kg/m3 = 6.2428 lb/ft3 (100 / 16.018463374 == 6.24279606...,
        # rounded to 4dp).
        pytest.param(
            100, "kg_per_cubic_meter", "lb_per_cubic_ft", 6.2428, id="kg_per_cubic_meter_to_lb_per_cubic_ft_rounded"
        ),
    ],
)
async def test_density_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/density",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["kg_per_cubic_meter", "g_per_cubic_cm", "lb_per_cubic_ft", "kg_per_liter"],
)
async def test_all_four_density_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    density units as both a valid `from_unit` and a valid `to_unit`
    cheaply: any unit not in the router's `units` dict would 400 with
    "Invalid unit" instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/density",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_density_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/density",
        json={"value": 1, "from_unit": "oz_per_gallon", "to_unit": "kg_per_cubic_meter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_density_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/density",
        json={"value": 1, "from_unit": "kg_per_cubic_meter", "to_unit": "oz_per_gallon"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_density_missing_value_field_rejected_with_422(client, api_key):
    """`DensityConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/density",
        json={"from_unit": "lb_per_cubic_ft", "to_unit": "kg_per_cubic_meter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_density_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/density",
        json={"value": 1, "to_unit": "kg_per_cubic_meter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_density_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/density",
        json={"value": 1, "from_unit": "lb_per_cubic_ft"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_density_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/density",
        json={"value": 1, "from_unit": "lb_per_cubic_ft", "to_unit": "kg_per_cubic_meter"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_density_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/density",
        json={"value": 1, "from_unit": "lb_per_cubic_ft", "to_unit": "kg_per_cubic_meter"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_density_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `DensityConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "lb_per_cubic_ft", "to_unit": "kg_per_cubic_meter"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/density",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_density_round_trip_lb_per_cubic_ft_to_kg_per_cubic_meter_to_lb_per_cubic_ft_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover the original
    value exactly here: 10 * 16.018463374 / 1.0 == 160.18463374 exactly,
    rounded to 4dp == 160.1846; converting 160.1846 back (160.1846 * 1.0 /
    16.018463374 == 9.99999...) recovers the original within a small
    tolerance."""
    first = await client.post(
        "/v1/unit_converters/density",
        json={"value": 10, "from_unit": "lb_per_cubic_ft", "to_unit": "kg_per_cubic_meter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 160.1846

    second = await client.post(
        "/v1/unit_converters/density",
        json={"value": intermediate, "from_unit": "kg_per_cubic_meter", "to_unit": "lb_per_cubic_ft"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-3)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 1 cubic_meter_per_hour = 0.2778 liter_per_second (0.277777778
        # rounded to 4dp).
        pytest.param(
            1,
            "cubic_meter_per_hour",
            "liter_per_second",
            0.2778,
            id="cubic_meter_per_hour_to_liter_per_second_rounded",
        ),
        # 1 gallon_per_minute = 0.0631 liter_per_second (0.0630901964
        # rounded to 4dp).
        pytest.param(
            1, "gallon_per_minute", "liter_per_second", 0.0631, id="gallon_per_minute_to_liter_per_second_rounded"
        ),
        # 1 cubic_foot_per_minute = 0.4719 liter_per_second (0.4719474432
        # rounded to 4dp).
        pytest.param(
            1,
            "cubic_foot_per_minute",
            "liter_per_second",
            0.4719,
            id="cubic_foot_per_minute_to_liter_per_second_rounded",
        ),
        # 1 liter_per_second = 3.6 cubic_meter_per_hour (1.0 / 0.277777778
        # == 3.60000000..., rounded to 4dp).
        pytest.param(
            1, "liter_per_second", "cubic_meter_per_hour", 3.6, id="liter_per_second_to_cubic_meter_per_hour"
        ),
        # 1 gallon_per_minute = 0.1337 cubic_foot_per_minute
        # (0.0630901964 / 0.4719474432 == 0.13368055..., rounded to 4dp).
        pytest.param(
            1,
            "gallon_per_minute",
            "cubic_foot_per_minute",
            0.1337,
            id="gallon_per_minute_to_cubic_foot_per_minute_rounded",
        ),
    ],
)
async def test_flow_rate_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["liter_per_second", "cubic_meter_per_hour", "gallon_per_minute", "cubic_foot_per_minute"],
)
async def test_all_four_flow_rate_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    flow rate units as both a valid `from_unit` and a valid `to_unit`
    cheaply: any unit not in the router's `units` dict would 400 with
    "Invalid unit" instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_flow_rate_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": 1, "from_unit": "barrel_per_day", "to_unit": "liter_per_second"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_flow_rate_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": 1, "from_unit": "liter_per_second", "to_unit": "barrel_per_day"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_flow_rate_missing_value_field_rejected_with_422(client, api_key):
    """`FlowRateConvertRequest.value` has no default, so omitting it
    entirely fails Pydantic validation before the handler body ever runs -
    a 422, distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"from_unit": "gallon_per_minute", "to_unit": "cubic_foot_per_minute"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_flow_rate_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": 1, "to_unit": "cubic_foot_per_minute"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_flow_rate_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": 1, "from_unit": "gallon_per_minute"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_flow_rate_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": 1, "from_unit": "gallon_per_minute", "to_unit": "cubic_foot_per_minute"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_flow_rate_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": 1, "from_unit": "gallon_per_minute", "to_unit": "cubic_foot_per_minute"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_flow_rate_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `FlowRateConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "gallon_per_minute", "to_unit": "cubic_foot_per_minute"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/flow_rate",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_flow_rate_round_trip_gallon_per_minute_to_cubic_foot_per_minute_to_gallon_per_minute_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover (approximately)
    the original value. Independently verified: 10 * 0.0630901964 /
    0.4719474432 == 1.33680555..., which the router rounds to 4dp
    (1.3368); converting 1.3368 back (1.3368 * 0.4719474432 /
    0.0630901964 == 9.99978...) recovers the original within a small
    tolerance."""
    first = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": 10, "from_unit": "gallon_per_minute", "to_unit": "cubic_foot_per_minute"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 1.3368

    second = await client.post(
        "/v1/unit_converters/flow_rate",
        json={"value": intermediate, "from_unit": "cubic_foot_per_minute", "to_unit": "gallon_per_minute"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-3)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 180 degrees = pi radians (180 * 0.017453292519943 ==
        # 3.14159265..., rounded to 4dp).
        pytest.param(180, "degree", "radian", 3.1416, id="180_degree_to_radian"),
        # 1 turn = 2*pi radians (6.283185307179586 rounded to 4dp).
        pytest.param(1, "turn", "radian", 6.2832, id="turn_to_radian_rounded"),
        # 1 turn = 360 degrees (6.283185307179586 / 0.017453292519943 ==
        # 360.0 exactly, by definition of these two router constants).
        pytest.param(1, "turn", "degree", 360.0, id="turn_to_degree"),
        # 100 gradians = 90 degrees (100 * 0.015707963267949 /
        # 0.017453292519943 == 90.0 exactly, by definition of these two
        # router constants).
        pytest.param(100, "gradian", "degree", 90.0, id="100_gradian_to_degree"),
        # 1 radian = 57.2958 degrees (1.0 / 0.017453292519943 ==
        # 57.29577951..., rounded to 4dp).
        pytest.param(1, "radian", "degree", 57.2958, id="radian_to_degree_rounded"),
    ],
)
async def test_angle_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/angle",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize(
    "unit",
    ["degree", "radian", "gradian", "turn"],
)
async def test_all_four_angle_units_accepted_as_from_and_to_unit(client, api_key, unit):
    """Identity conversion (unit -> itself) exercises every one of the 4
    angle units as both a valid `from_unit` and a valid `to_unit` cheaply:
    any unit not in the router's `units` dict would 400 with "Invalid unit"
    instead of returning a 200 with `result == value`."""
    resp = await client.post(
        "/v1/unit_converters/angle",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_angle_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/angle",
        json={"value": 1, "from_unit": "arcminute", "to_unit": "degree"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_angle_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/angle",
        json={"value": 1, "from_unit": "degree", "to_unit": "arcminute"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_angle_missing_value_field_rejected_with_422(client, api_key):
    """`AngleConvertRequest.value` has no default, so omitting it entirely
    fails Pydantic validation before the handler body ever runs - a 422,
    distinct from the handler's own 400 "Invalid unit" path."""
    resp = await client.post(
        "/v1/unit_converters/angle",
        json={"from_unit": "degree", "to_unit": "radian"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_angle_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/angle",
        json={"value": 1, "to_unit": "radian"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_angle_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/angle",
        json={"value": 1, "from_unit": "degree"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_angle_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/angle",
        json={"value": 1, "from_unit": "degree", "to_unit": "radian"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_angle_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/angle",
        json={"value": 1, "from_unit": "degree", "to_unit": "radian"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_angle_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `AngleConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "degree", "to_unit": "radian"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/angle",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_angle_round_trip_degree_to_radian_to_degree_returns_approximately_original(
    client, api_key
):
    """A round trip through two conversions should recover (approximately)
    the original value. Independently verified: 10 * 0.017453292519943 /
    1.0 == 0.17453292519943, which the router rounds to 4dp (0.1745);
    converting 0.1745 back (0.1745 * 1.0 / 0.017453292519943 ==
    9.99810...) does NOT recover the exact original at 4dp precision - the
    assertion below uses a tolerance rather than exact equality, mirroring
    the volume round-trip test above."""
    first = await client.post(
        "/v1/unit_converters/angle",
        json={"value": 10, "from_unit": "degree", "to_unit": "radian"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 0.1745

    second = await client.post(
        "/v1/unit_converters/angle",
        json={"value": intermediate, "from_unit": "radian", "to_unit": "degree"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-2)


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # 30 mpg = 12.7543 km/L (30 * 0.425143707 == 12.75431121, rounded to
        # 4dp per the router's own `round(result, 4)`).
        pytest.param(30, "mpg", "km_per_liter", 12.7543, id="mpg_to_kpl"),
        # 10 km/L = 23.5215 mpg (10 / 0.425143707 == 23.52145985..., rounded
        # to 4dp).
        pytest.param(10, "km_per_liter", "mpg", 23.5215, id="kpl_to_mpg"),
        # 10 km/L = 10.0 L/100km (100 / 10 == 10.0 exactly - the reciprocal
        # formula, not a multiplier).
        pytest.param(10, "km_per_liter", "l_per_100km", 10.0, id="kpl_to_l100"),
        # 8 L/100km = 12.5 km/L (100 / 8 == 12.5 exactly, inverse of the
        # above reciprocal formula).
        pytest.param(8, "l_per_100km", "km_per_liter", 12.5, id="l100_to_kpl"),
        # 30 mpg = 7.8405 L/100km, composed through km_per_liter as the
        # internal base: 100 / (30 * 0.425143707) == 7.84048..., rounded to
        # 4dp.
        pytest.param(30, "mpg", "l_per_100km", 7.8405, id="mpg_to_l100"),
        # 8 L/100km = 29.4018 mpg, composed the other direction: 100 /
        # (8 * 0.425143707) == 29.40179..., rounded to 4dp.
        pytest.param(8, "l_per_100km", "mpg", 29.4018, id="l100_to_mpg"),
    ],
)
async def test_fuel_efficiency_conversion_with_exact_expected_values(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["from_unit"] == from_unit
    assert body["to_unit"] == to_unit
    assert body["result"] == expected


@pytest.mark.parametrize("unit", ["mpg", "km_per_liter", "l_per_100km"])
async def test_all_three_fuel_efficiency_units_accepted_as_from_and_to_unit(
    client, api_key, unit
):
    """Identity conversion (unit -> itself) exercises every one of the 3
    fuel-efficiency units as both a valid `from_unit` and a valid `to_unit`
    cheaply: any unit not in the router's `valid_units` set would 400 with
    "Invalid unit" instead of returning a 200 with `result == value`. Unlike
    every multiplier-through-origin batch above, this identity path is an
    explicit `from_unit == to_unit` short-circuit in the handler (not a flat
    `units` dict divide-by-self), since fuel efficiency's conversions are
    reciprocal, not multiplicative."""
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 42, "from_unit": unit, "to_unit": unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 42.0


async def test_fuel_efficiency_invalid_from_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 1, "from_unit": "liters_per_mile", "to_unit": "mpg"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_fuel_efficiency_invalid_to_unit_returns_400(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 1, "from_unit": "mpg", "to_unit": "liters_per_mile"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


async def test_fuel_efficiency_missing_value_field_rejected_with_422(client, api_key):
    """`FuelEfficiencyConvertRequest.value` has no default, so omitting it
    entirely fails Pydantic validation before the handler body ever runs -
    a 422, distinct from the handler's own 400 "Invalid unit"/zero-value
    paths."""
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"from_unit": "mpg", "to_unit": "km_per_liter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_fuel_efficiency_missing_from_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 1, "to_unit": "km_per_liter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_fuel_efficiency_missing_to_unit_field_rejected_with_422(client, api_key):
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 1, "from_unit": "mpg"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_fuel_efficiency_invalid_api_key_value_rejected_with_envelope(client):
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 1, "from_unit": "mpg", "to_unit": "km_per_liter"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_fuel_efficiency_missing_api_key_header_rejected(client):
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 1, "from_unit": "mpg", "to_unit": "km_per_liter"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize("field", ["from_unit", "to_unit"])
async def test_fuel_efficiency_empty_string_unit_returns_400(client, api_key, field):
    """An empty string is a structurally valid `str` for Pydantic (no
    `min_length` constraint on `FuelEfficiencyConvertRequest`), so it passes
    validation and reaches the handler body - where `"" not in valid_units`
    correctly falls into the same 400 "Invalid unit" path as any other
    unrecognized unit name, not a 422."""
    payload = {"value": 1, "from_unit": "mpg", "to_unit": "km_per_liter"}
    payload[field] = ""
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid unit"
    assert body["error"]["code"] == "HTTP_ERROR"


@pytest.mark.parametrize(
    "value, from_unit, to_unit",
    [
        # `l_per_100km` as `from_unit` divides directly by `request.value`
        # (`km_per_liter = 100 / value`) - a value of exactly 0 hits that
        # division before any unit-conversion math happens.
        pytest.param(0, "l_per_100km", "mpg", id="zero_l100_to_mpg"),
        pytest.param(0, "l_per_100km", "km_per_liter", id="zero_l100_to_kpl"),
        # `km_per_liter` as `from_unit` with value 0 converts cleanly to an
        # internal `km_per_liter` base of 0.0 (no division on that leg), but
        # landing on `l_per_100km` as `to_unit` then divides BY that zero
        # base (`100 / km_per_liter`), hitting the second division site.
        pytest.param(0, "km_per_liter", "l_per_100km", id="zero_kpl_to_l100"),
        # Same second-division-site case, but arriving at a zero
        # `km_per_liter` base via `mpg` (`0 * MPG_TO_KPL == 0.0`) instead of
        # directly.
        pytest.param(0, "mpg", "l_per_100km", id="zero_mpg_to_l100"),
    ],
)
async def test_fuel_efficiency_zero_value_on_divide_by_value_path_returns_400_not_500(
    client, api_key, value, from_unit, to_unit
):
    """`convert_fuel_efficiency` is reciprocal, not multiplier-through-origin
    - a `value` of 0 on any leg that divides BY that value (either the
    initial `l_per_100km` `from_unit` conversion, or the second leg landing
    on `l_per_100km` as `to_unit` after the first leg produces a zero
    `km_per_liter` base) is caught explicitly and returns a clean 400, never
    a 500 and never an `inf`/`nan` serialized into the JSON body (Python's
    bare `100 / 0` raises `ZeroDivisionError`, which the handler catches
    before it can propagate to the generic `except Exception` 500 path)."""
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Invalid value: cannot convert a value of zero for this unit"
    assert body["error"]["code"] == "HTTP_ERROR"
    assert "inf" not in resp.text.lower()
    assert "nan" not in resp.text.lower()


@pytest.mark.parametrize(
    "value, from_unit, to_unit, expected",
    [
        # A 0 value on a leg that does NOT divide by the input (mpg/km_per_liter
        # as `from_unit` multiply rather than divide) converts cleanly to
        # 0.0 - no ZeroDivisionError, unlike the divide-by-value cases above.
        pytest.param(0, "mpg", "km_per_liter", 0.0, id="zero_mpg_to_kpl"),
        pytest.param(0, "km_per_liter", "mpg", 0.0, id="zero_kpl_to_mpg"),
    ],
)
async def test_fuel_efficiency_zero_value_on_non_divide_by_value_path_returns_200(
    client, api_key, value, from_unit, to_unit, expected
):
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": value, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == expected


async def test_fuel_efficiency_identity_conversion_with_zero_value_returns_zero_unchanged(
    client, api_key
):
    """`from_unit == to_unit` is an explicit short-circuit in the handler
    that returns `request.value` unchanged before any reciprocal math runs
    - so even `l_per_100km` -> `l_per_100km` (which would otherwise divide
    BY the input value on the `from_unit` leg) returns 200 with `result ==
    0.0`, not the 400 that a genuine cross-unit zero-value conversion would
    hit (see
    `test_fuel_efficiency_zero_value_on_divide_by_value_path_returns_400_not_500`'s
    `zero_l100_to_mpg`/`zero_l100_to_kpl` cases)."""
    resp = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 0, "from_unit": "l_per_100km", "to_unit": "l_per_100km"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["result"] == 0.0


async def test_fuel_efficiency_round_trip_mpg_to_kpl_to_mpg_returns_original(
    client, api_key
):
    """A round trip through two conversions should recover (approximately)
    the original value. Independently verified: 30 * 0.425143707 ==
    12.75431121, which the router rounds to 4dp (12.7543); converting
    12.7543 back (12.7543 / 0.425143707 == 29.99998...) recovers the
    original to within a small tolerance, mirroring the weight/area
    round-trip tests above."""
    first = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 30, "from_unit": "mpg", "to_unit": "km_per_liter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 12.7543

    second = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": intermediate, "from_unit": "km_per_liter", "to_unit": "mpg"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(30.0, abs=1e-3)


async def test_fuel_efficiency_round_trip_kpl_to_l100_to_kpl_returns_original(
    client, api_key
):
    """Round trip through the genuinely reciprocal leg (`km_per_liter` <->
    `l_per_100km`, both computed as `100 / value` in each direction):
    10 km/L -> 10.0 L/100km -> 10.0 km/L recovers the original exactly here,
    since `100 / 10 == 10.0` is exact in both directions with no rounding
    error to accumulate."""
    first = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": 10, "from_unit": "km_per_liter", "to_unit": "l_per_100km"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert first.status_code == 200
    intermediate = first.json()["result"]
    assert intermediate == 10.0

    second = await client.post(
        "/v1/unit_converters/fuel_efficiency",
        json={"value": intermediate, "from_unit": "l_per_100km", "to_unit": "km_per_liter"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert second.status_code == 200
    final = second.json()["result"]
    assert final == pytest.approx(10.0, abs=1e-3)
