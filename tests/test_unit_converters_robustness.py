"""Coverage for the three robustness fixes applied across all 18 Unit
Converter endpoints in `app/routers/unit_converters.py` and their request
models in `app/models/unit_converters.py` (length, temperature, weight,
area, volume, speed, time, data, energy, power, pressure, frequency, force,
torque, density, flow_rate, angle, fuel_efficiency):

1. **Error-message leak fix** - every endpoint's generic `except Exception`
   handler now raises `api_error(500, "Failed to convert <type>",
   "CONVERSION_FAILED")` (`app/shared/responses.py`) instead of leaking
   `str(e)` into the response `detail`.
2. **Bounded `value`** - every `*ConvertRequest.value` field now carries
   `Field(allow_inf_nan=False, ge=-1e15, le=1e15)`.
3. **inf/nan rejection** - the same `allow_inf_nan=False` constraint, now on
   all 18 models (previously only `FuelEfficiencyConvertRequest` had it -
   see `tests/test_unit_converters.py::test_fuel_efficiency_inf_nan_value_rejected_with_422_not_500`
   for that endpoint's pre-existing coverage of this same mechanism).

This file intentionally does NOT re-derive each endpoint's conversion
formula/expected numeric result - `tests/test_unit_converters.py` already
owns that (exact multiplier/affine/reciprocal math per unit type, per its
own module docstring) and continues to pass unmodified (see the full-suite
run reported alongside this file). Per this task's brief, the "valid
in-range value still returns 200" regression check below reuses the
identity-conversion pattern already established in
`tests/test_unit_converters.py` (e.g.
`test_all_seven_units_accepted_as_from_and_to_unit`,
`test_all_three_temperature_units_accepted_as_from_and_to_unit`, etc. -
`unit -> same unit` returns `result == value` regardless of the
per-endpoint formula) rather than re-typing 18 separate multiplier/affine
tables, since `value` bounding is orthogonal to conversion math and an
identity round-trip is a formula-agnostic way to confirm the request still
reaches the handler and returns 200 with the correct result post-fix.

Every unit name below is confirmed directly against the live `units`
dict/`valid_units` set literals in `app/routers/unit_converters.py` (not
guessed from the docstring) as of this session.

`from_unit`/`to_unit` are irrelevant to whether Pydantic accepts or rejects
`value` (field-level `Field(...)` validation runs independently of other
fields and before the handler body ever executes), so the bound/inf/nan
tests below all use each endpoint's own base unit as both `from_unit` and
`to_unit` for simplicity - what's under test is purely the `value`
constraint, never reached by the handler at all for the invalid cases.

`fuel_efficiency` is the one endpoint where a same-unit (`mpg` -> `mpg`)
payload does NOT exercise the same code path as every other endpoint for
the forced-500 test below: `convert_fuel_efficiency` has an
`from_unit == to_unit` identity short-circuit that returns *before* its own
`try`/`except Exception` block even starts (see
`app/routers/unit_converters.py::convert_fuel_efficiency` lines ~504-514),
so `round()` there is unguarded by `except Exception` and a forced failure
would hit the test app's generic 500 handler ("Internal server error") that
`tests/conftest.py::build_test_app` calls to catch a runtime-only
`RequestValidationError`, but the real `app.main` at line
`unhandled_exception_handler` still returns to a generic error - the same
non-leaking shape either way. To exercise the endpoint's own
`api_error(500, "Failed to convert fuel efficiency", "CONVERSION_FAILED")`
path specifically (mirroring every other endpoint 1:1), the forced-500 test
uses a genuine cross-unit `mpg` -> `km_per_liter` payload for that one row
instead of the identity pair used for the valid-value regression check.

**Forcing a genuine 500 without mocking `api_error` itself:** none of the
18 handlers has a reachable internal failure mode with valid, in-bound,
finite input post-fix - every `units` dict multiplier is a nonzero
constant, temperature/fuel_efficiency's affine/reciprocal math has its own
explicit guards, and `value` is now bounded/finite before the handler ever
runs. So a real 500 is deliberately forced the same way for every endpoint
below: `monkeypatch.setattr(router_module, "round", <raises RuntimeError
with a fake secret in the message>, raising=False)`. This works because
`round(...)` inside each handler is an unqualified name (Python `LOAD_GLOBAL`
bytecode - module globals checked before builtins), so shadowing `round` on
the `app.routers.unit_converters` module object intercepts every handler's
own `round(result, 4)` call and forces it to raise - without touching
`app/routers/unit_converters.py` or `app/models/unit_converters.py` on disk,
and without weakening the assertion to merely checking `api_error()`'s
return shape in isolation. This drives a real exception through the real
`except Exception` branch of each real handler, through the real HTTP
layer, confirming end-to-end that the leaked-secret message never reaches
the response body - not just that `api_error()` would theoretically build a
safe envelope if used correctly.
"""
import pytest

# See tests/test_worker_retry.py's module docstring/comment for why this is
# pinned to the session-scoped loop (Motor's shared `app.core.database.db`
# client) - matches tests/test_unit_converters.py's own pytestmark.
pytestmark = pytest.mark.asyncio(loop_scope="session")


# (key, path, valid_from_unit, valid_to_unit, "Failed to convert <type>" message)
# valid_from_unit == valid_to_unit for every row (identity conversion) except
# where noted - see module docstring for why fuel_efficiency's forced-500
# test overrides this with a non-identity pair.
ENDPOINTS = [
    ("length", "/v1/unit_converters/length", "meter", "meter", "Failed to convert length"),
    (
        "temperature",
        "/v1/unit_converters/temperature",
        "celsius",
        "celsius",
        "Failed to convert temperature",
    ),
    ("weight", "/v1/unit_converters/weight", "kilogram", "kilogram", "Failed to convert weight"),
    (
        "area",
        "/v1/unit_converters/area",
        "square_meter",
        "square_meter",
        "Failed to convert area",
    ),
    ("volume", "/v1/unit_converters/volume", "liter", "liter", "Failed to convert volume"),
    (
        "speed",
        "/v1/unit_converters/speed",
        "meter_per_second",
        "meter_per_second",
        "Failed to convert speed",
    ),
    ("time", "/v1/unit_converters/time", "second", "second", "Failed to convert time"),
    ("data", "/v1/unit_converters/data", "byte", "byte", "Failed to convert data"),
    ("energy", "/v1/unit_converters/energy", "joule", "joule", "Failed to convert energy"),
    ("power", "/v1/unit_converters/power", "watt", "watt", "Failed to convert power"),
    (
        "pressure",
        "/v1/unit_converters/pressure",
        "pascal",
        "pascal",
        "Failed to convert pressure",
    ),
    (
        "frequency",
        "/v1/unit_converters/frequency",
        "hertz",
        "hertz",
        "Failed to convert frequency",
    ),
    ("force", "/v1/unit_converters/force", "newton", "newton", "Failed to convert force"),
    (
        "torque",
        "/v1/unit_converters/torque",
        "newton_meter",
        "newton_meter",
        "Failed to convert torque",
    ),
    (
        "density",
        "/v1/unit_converters/density",
        "kg_per_cubic_meter",
        "kg_per_cubic_meter",
        "Failed to convert density",
    ),
    (
        "flow_rate",
        "/v1/unit_converters/flow_rate",
        "liter_per_second",
        "liter_per_second",
        "Failed to convert flow rate",
    ),
    ("angle", "/v1/unit_converters/angle", "radian", "radian", "Failed to convert angle"),
    (
        "fuel_efficiency",
        "/v1/unit_converters/fuel_efficiency",
        "mpg",
        "mpg",
        "Failed to convert fuel efficiency",
    ),
]

ENDPOINT_IDS = [row[0] for row in ENDPOINTS]


@pytest.mark.parametrize(
    "key, path, from_unit, to_unit, message", ENDPOINTS, ids=ENDPOINT_IDS
)
async def test_value_above_upper_bound_rejected_with_422(
    client, api_key, key, path, from_unit, to_unit, message
):
    """`value` above the new `le=1e15` cap is rejected at request-validation
    time - a 422, never reaching the handler (so never a 500, never
    silently accepted/clamped)."""
    resp = await client.post(
        path,
        json={"value": 1e15 + 1e9, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, f"{key}: expected 422, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "key, path, from_unit, to_unit, message", ENDPOINTS, ids=ENDPOINT_IDS
)
async def test_value_below_lower_bound_rejected_with_422(
    client, api_key, key, path, from_unit, to_unit, message
):
    """`value` below the new `ge=-1e15` floor is rejected at
    request-validation time - a 422, never reaching the handler."""
    resp = await client.post(
        path,
        json={"value": -1e15 - 1e9, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, f"{key}: expected 422, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.parametrize(
    "key, path, from_unit, to_unit, message", ENDPOINTS, ids=ENDPOINT_IDS
)
async def test_value_infinity_rejected_with_422_not_500(
    client, api_key, key, path, from_unit, to_unit, message
):
    """A raw JSON body with the literal (non-standard, but
    `json.loads`-accepted) `Infinity` token for `value` is rejected by
    `allow_inf_nan=False` with a clean 422 - not accepted, and not a 500
    from `inf` surviving into `round()`/response serialization. Sent as raw
    bytes (not a Python dict with `float("inf")`) since `httpx`/`TestClient`
    posting a dict containing `float("inf")` would still serialize to this
    same non-standard `Infinity` token via `json.dumps`, but writing it as
    literal JSON text here makes the on-the-wire payload explicit."""
    raw_body = (
        '{"value": Infinity, "from_unit": "%s", "to_unit": "%s"}' % (from_unit, to_unit)
    ).encode()
    resp = await client.post(
        path,
        content=raw_body,
        headers={"X-API-Key": api_key["key"], "Content-Type": "application/json"},
    )
    assert resp.status_code == 422, f"{key}: expected 422, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "inf" not in resp.text.lower()


@pytest.mark.parametrize(
    "key, path, from_unit, to_unit, message", ENDPOINTS, ids=ENDPOINT_IDS
)
async def test_value_nan_rejected_with_422_not_500(
    client, api_key, key, path, from_unit, to_unit, message
):
    """Same as the `Infinity` test above, but for the `NaN` token -
    `allow_inf_nan=False` rejects it with a clean 422 as well."""
    raw_body = (
        '{"value": NaN, "from_unit": "%s", "to_unit": "%s"}' % (from_unit, to_unit)
    ).encode()
    resp = await client.post(
        path,
        content=raw_body,
        headers={"X-API-Key": api_key["key"], "Content-Type": "application/json"},
    )
    assert resp.status_code == 422, f"{key}: expected 422, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
    assert "nan" not in resp.text.lower()


@pytest.mark.parametrize(
    "key, path, from_unit, to_unit, message", ENDPOINTS, ids=ENDPOINT_IDS
)
async def test_valid_in_range_value_still_returns_200_with_correct_result(
    client, api_key, key, path, from_unit, to_unit, message
):
    """Regression check for the bounding fix: an ordinary, comfortably
    in-range value (10, far below the new `1e15` cap) on an identity
    conversion (`unit -> same unit`) still returns 200 with `result ==
    value`, exactly as it did before this session's fixes - mirrors the
    identity-conversion convention already established per unit type in
    `tests/test_unit_converters.py` (see that file's own
    `test_all_seven_units_accepted_as_from_and_to_unit` and siblings)."""
    resp = await client.post(
        path,
        json={"value": 10, "from_unit": from_unit, "to_unit": to_unit},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, f"{key}: expected 200, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["result"] == 10.0


@pytest.mark.parametrize(
    "key, path, from_unit, to_unit, message", ENDPOINTS, ids=ENDPOINT_IDS
)
async def test_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch, key, path, from_unit, to_unit, message
):
    """Directly exercises the error-message-leak fix through the real HTTP
    layer (not just `api_error()`'s return shape in isolation): forces a
    genuine exception inside each handler's own `try` block by shadowing
    the module-global `round` (see module docstring for why this works and
    why it doesn't require touching the router/model source), and confirms
    the response is the new safe `api_error(500, "Failed to convert
    <type>", "CONVERSION_FAILED")` envelope - never the old `f"Error
    converting <type>: {str(e)}"` shape, and never any fragment of the
    forced exception's message (including a fake secret planted in it) in
    the response body.

    `fuel_efficiency` uses `mpg` -> `km_per_liter` (not the identity `mpg`
    -> `mpg` pair used by the valid-value regression test above) - see
    module docstring for why the identity path bypasses that endpoint's own
    `try`/`except Exception` entirely."""
    import app.routers.unit_converters as router_module

    def _boom(*args, **kwargs):
        raise RuntimeError(
            "SECRET_MARKER_never_reach_the_client db_password=hunter2 at "
            "/app/routers/unit_converters.py"
        )

    monkeypatch.setattr(router_module, "round", _boom, raising=False)

    request_from, request_to = from_unit, to_unit
    if key == "fuel_efficiency":
        request_from, request_to = "mpg", "km_per_liter"

    resp = await client.post(
        path,
        json={"value": 10, "from_unit": request_from, "to_unit": request_to},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, f"{key}: expected 500, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == message
    assert body["error"]["code"] == "CONVERSION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "unit_converters.py" not in resp.text
