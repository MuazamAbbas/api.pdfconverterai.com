"""Coverage for `POST /v1/miscellaneous/qr_code` (Handbook Part I.2 - Tier 1,
no job queue, plain sync endpoint like the same module's `/timestamp`), plus
its request validation and `verify_api_key` auth behavior.

Against the real `miscellaneous` router (mounted in
`tests/conftest.py::build_test_app`) and real Mongo (via the `api_key`
fixture) - no mocking of `verify_api_key`, matching the rest of this test
suite's convention of exercising the real dependency against a real,
fixture-created API key document rather than overriding it.

The endpoint returns raw PNG bytes (`Response(media_type="image/png")`), not
a JSON envelope, so the happy-path assertions decode the response body with
Pillow to confirm it is an actual, valid PNG image - not just a 200 with an
opaque byte blob.
"""
import io

import pytest
from PIL import Image

# See tests/test_worker_retry.py's module docstring/comment for why this is
# pinned to the session-scoped loop (Motor's shared `app.core.database.db`
# client).
pytestmark = pytest.mark.asyncio(loop_scope="session")

_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def _assert_valid_png(content: bytes) -> Image.Image:
    assert content.startswith(_PNG_MAGIC)
    image = Image.open(io.BytesIO(content))
    image.load()  # forces full decode, not just header parsing
    assert image.format == "PNG"
    return image


async def test_qr_code_default_request_returns_valid_png(client, api_key):
    resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json={"text": "https://pdfconverterai.com"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    assert len(resp.content) > 0
    image = _assert_valid_png(resp.content)
    assert image.width > 0 and image.height > 0


@pytest.mark.parametrize("error_correction", ["L", "M", "Q", "H"])
async def test_qr_code_accepts_each_valid_error_correction_level(client, api_key, error_correction):
    resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json={"text": "hello world", "error_correction": error_correction},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "image/png"
    _assert_valid_png(resp.content)


async def test_qr_code_custom_size_produces_a_larger_image_than_default(client, api_key):
    """`size` maps to `box_size` derived from the QR's actual module count
    (see `app/routers/miscellaneous.py::generate_qr_code`), so a much larger
    requested `size` should produce a visibly larger PNG for the same text -
    not an exact pixel match (module/version count also affects the final
    dimensions), just a strictly bigger image."""
    text = "same text, two sizes"
    default_resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json={"text": text},
        headers={"X-API-Key": api_key["key"]},
    )
    large_resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json={"text": text, "size": 900},
        headers={"X-API-Key": api_key["key"]},
    )
    assert default_resp.status_code == 200
    assert large_resp.status_code == 200
    default_image = _assert_valid_png(default_resp.content)
    large_image = _assert_valid_png(large_resp.content)
    assert large_image.width > default_image.width
    assert large_image.height > default_image.height


async def test_qr_code_capacity_overflow_returns_400_not_500(client, api_key):
    """At `error_correction="H"`, 2000 chars (the max `text` length allowed
    by `QRCodeRequest`) exceeds every QR version's (1-40) capacity, so
    `qr.make(fit=True)` raises a bare `ValueError` internally
    (`app/routers/miscellaneous.py::generate_qr_code`). The fix catches that
    and re-raises as a 400 with a clear, client-facing message - not the
    generic 500 `unhandled_exception_handler` would otherwise produce for an
    uncaught exception, and not a leak of the raw
    `ValueError("Invalid version (was 41, expected 1 to 40)")` message or
    any other exception-implementation detail (type name, traceback, file
    path)."""
    resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json={"text": "x" * 2000, "error_correction": "H"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "HTTP_ERROR"

    message = body["message"]
    assert "too long to encode" in message
    assert "error correction level" in message
    # Must not leak the underlying library's raw exception/implementation
    # details (Handbook Part C.10 - never leak a stack trace).
    assert "ValueError" not in message
    assert "Invalid version" not in message
    assert "Traceback" not in message
    assert ".py" not in message


async def test_qr_code_size_tracks_requested_size_after_box_size_fix(client, api_key):
    """Before the fix, `box_size` was computed as a fixed `size // 30`
    regardless of the QR's actual module count, so a `size=1000` request
    for text needing a large QR version could balloon the output to several
    times the requested size. The fix derives `box_size` from
    `qr.modules_count` (set by `qr.make(fit=True)`) instead, so the output
    PNG should land close to the requested `size` - not exactly `size`
    (integer module-count division still applies), but within one module's
    worth of pixels of it, and nowhere near the old ballooned dimensions."""
    text = "x" * 2000
    size = 1000
    resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json={"text": text, "size": size, "error_correction": "L"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200
    image = _assert_valid_png(resp.content)

    # Reproduce the fix's own box_size math against the real `qrcode`
    # library for this exact (text, error_correction, size) combination to
    # get a precise expected dimension, rather than hardcoding a guessed
    # pixel count.
    import qrcode as _qrcode

    qr = _qrcode.QRCode(error_correction=_qrcode.constants.ERROR_CORRECT_L, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    total_modules = qr.modules_count + 2 * qr.border
    expected_box_size = max(1, size // total_modules)
    expected_dim = total_modules * expected_box_size

    assert image.width == expected_dim
    assert image.height == expected_dim
    # Sanity bounds: within one module's worth of pixels of the requested
    # size (integer division can only undershoot, never overshoot by more
    # than that), and nowhere near the old size // 30 box_size ballooning
    # (which produced roughly 5x the requested size for this input).
    assert abs(image.width - size) <= total_modules
    assert image.width < size * 1.5


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"text": ""}, id="empty_text"),
        pytest.param({"text": "x" * 2001}, id="text_over_max_length"),
        pytest.param({"text": "valid", "size": 99}, id="size_below_minimum"),
        pytest.param({"text": "valid", "size": 1001}, id="size_above_maximum"),
        pytest.param({"text": "valid", "error_correction": "X"}, id="invalid_error_correction"),
    ],
)
async def test_qr_code_rejects_invalid_payloads_with_validation_envelope(client, api_key, payload):
    """Each of these fails `QRCodeRequest`'s Pydantic field constraints
    (`text`'s `min_length=1, max_length=2000`, `size`'s `ge=100, le=1000`,
    `error_correction`'s `Literal["L","M","Q","H"]`) before the handler body
    ever runs, so it's a 422 with the same Handbook Part C.5 envelope every
    other validation error in this suite gets (see
    `tests/test_files_jobs_text_flow.py::test_word_count_endpoint_rejects_empty_text`
    for the same pattern - a *present, valid* API key plus an invalid body)."""
    resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json=payload,
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_qr_code_rejects_missing_text_field_entirely(client, api_key):
    """`text` has no default, so omitting the field entirely is also a 422,
    distinct from (but exercised alongside) the empty-string case above."""
    resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json={},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"


async def test_qr_code_invalid_api_key_value_rejected_with_envelope(client):
    """Mirrors
    `tests/test_files_jobs_pdf_flow.py::test_invalid_api_key_value_rejected_with_envelope`
    - a structurally present but unrecognized `X-API-Key` fails inside
    `verify_api_key` itself (`db.api_keys.find_one` returns nothing), which
    is a 403, not a 422."""
    resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json={"text": "hello"},
        headers={"X-API-Key": "not-a-real-key"},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert body["success"] is False
    assert "error" in body


async def test_qr_code_missing_api_key_header_rejected(client):
    """Mirrors
    `tests/test_files_jobs_pdf_flow.py::test_missing_api_key_header_rejected`
    - a structurally missing `X-API-Key` header fails FastAPI's own
    `Header(...)` requirement (a `RequestValidationError`), so it's a 422
    with the same envelope as an invalid request body, not `verify_api_key`'s
    403 (its function body never runs since the header parameter itself
    fails to resolve)."""
    resp = await client.post(
        "/v1/miscellaneous/qr_code",
        json={"text": "hello"},
    )
    assert resp.status_code == 422
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "VALIDATION_ERROR"
