"""Coverage for `POST /v1/unit_converters/length`,
`POST /v1/unit_converters/temperature`, and
`POST /v1/unit_converters/weight` (Handbook Part I.2 - Tier 1, no job
queue, plain sync endpoints), plus their unit-validation and
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
