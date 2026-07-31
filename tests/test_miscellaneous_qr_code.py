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
    """`size` maps to `box_size = max(1, size // 30)` (see
    `app/routers/miscellaneous.py::generate_qr_code`), so a much larger
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
