"""Coverage for the error-message-leak fix applied to `app/routers/
binary_tools.py`'s `text_to_binary` endpoint (`POST /v1/binary_tools/
text_to_binary`) as part of issue #31 Batch 1/3 (see `tests/
test_cyber_security_robustness.py` module docstring for the full lineage:
#27/PR#28 -> #29/PR#30 -> #31), plus coverage for the follow-up UTF-8
byte-encoding fix (`fix/binary-tools-utf8-encoding`,
`b7dcf2c`) that replaced the old per-codepoint `format(ord(char), '08b')`
(which overflowed `'08b'` for any codepoint above 255) with per-UTF-8-byte
encoding (`format(byte, '08b') for byte in request.text.encode('utf-8')`),
and for the new `binary_to_text` endpoint (`POST /v1/binary_tools/
binary_to_text`) added in the same commit.

Also covers the follow-up `fix/binary-tools-utf8-encoding` fast-follow that
bounds both request models (`text: str = Field(..., min_length=1,
max_length=10000)` on `TextToBinaryRequest`, matching `app/models/text.py`'s
`TextRequest` exactly; `binary: str = Field(..., min_length=1,
max_length=100000)` on `BinaryToTextRequest`) after a security-reviewer pass
flagged unbounded synchronous work inside `async def` handlers as a High
severity DoS risk on the 2-worker gunicorn config. The old manual
`if not request.text` / `if not request.binary` checks were removed as dead
code once `min_length=1` made them unreachable, matching the convention in
`app/routers/text.py` and `app/routers/miscellaneous.py`, which rely on
Pydantic's 422 alone with no manual empty-string check.

`text_to_binary` and `binary_to_text` have no separately-importable service
function to monkeypatch - their whole bodies are inline. The forced-500
tests shadow module-global builtin names (Python's `LOAD_GLOBAL` for a bare
name inside a function checks the function's own module `__dict__` before
falling back to builtins, so setting an attribute like `binary_tools.format`
or `binary_tools.bytes` on the module intercepts every call to that name
inside the target function's `try` block without touching the real builtin
used anywhere else) - the same spirit as `test_cyber_security_robustness.
py`'s `secrets.choice` patch, adapted for builtins with no importable owner
object. Since the `text_to_binary` fix moved from `ord()` to
`str.encode()` + `format()`, the forced-500 test for that endpoint now
shadows `format` (not `ord`, which the endpoint no longer calls).
"""
import pytest

import app.routers.binary_tools as binary_tools_router

pytestmark = pytest.mark.asyncio(loop_scope="session")

_SECRET_MARKER = "SECRET_MARKER_never_reach_the_client db_password=hunter2"


def _boom_format(*args, **kwargs):
    raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/binary_tools.py")


def _boom_bytes(*args, **kwargs):
    raise RuntimeError(f"{_SECRET_MARKER} at /app/routers/binary_tools.py")


async def test_text_to_binary_valid_request_returns_200_with_correct_result(client, api_key):
    resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": "Hi"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["binary"] == "01001000 01101001"


async def test_text_to_binary_non_ascii_emoji_uses_utf8_bytes_not_codepoints(client, api_key):
    """Proves the UTF-8-byte fix: under the old `ord(char)` bug, any
    codepoint above 255 (e.g. "\U0001F389" PARTY POPPER, codepoint 127881)
    would overflow `format(..., '08b')` into a binary string longer than 8
    bits, and non-ASCII multi-byte characters wouldn't round-trip via
    `binary_to_text` at all. Encoding via UTF-8 bytes means each group is
    always exactly 8 bits, and the number of groups equals the UTF-8 byte
    length of the input, not the codepoint count.
    """
    text = "café🎉"
    expected_bytes = text.encode("utf-8")
    # "café🎉" = c,a,f,é (2 bytes),🎉 (4 bytes) => 4 ascii-width chars worth
    # of bytes (1+1+1+2) + 4 = 9 bytes total, not 5 (the codepoint count).
    assert len(expected_bytes) == 9

    resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": text},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    groups = body["binary"].split()
    assert len(groups) == len(expected_bytes) == 9
    assert all(len(g) == 8 and set(g) <= {"0", "1"} for g in groups)
    assert groups == [format(b, "08b") for b in expected_bytes]


async def test_text_to_binary_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(binary_tools_router, "format", _boom_format, raising=False)
    resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": "Hi"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to convert text to binary"
    assert body["error"]["code"] == "BINARY_CONVERSION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "binary_tools.py" not in resp.text


# --- binary_to_text -------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Hi",
        "café",
        "naïve",
        "こんにちは",
        "مرحبا",
        "🎉",
        "café🎉",
    ],
)
async def test_binary_to_text_round_trips_utf8_via_text_to_binary(client, api_key, text):
    """Happy-path UTF-8 round trip through both endpoints: plain ASCII,
    accented Latin, non-Latin scripts (Japanese, Arabic), and a
    multi-byte emoji (\U0001F389 is 4 UTF-8 bytes / 4 groups)."""
    to_binary_resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": text},
        headers={"X-API-Key": api_key["key"]},
    )
    assert to_binary_resp.status_code == 200, to_binary_resp.text
    binary = to_binary_resp.json()["binary"]

    expected_byte_count = len(text.encode("utf-8"))
    assert len(binary.split()) == expected_byte_count

    to_text_resp = await client.post(
        "/v1/binary_tools/binary_to_text",
        json={"binary": binary},
        headers={"X-API-Key": api_key["key"]},
    )
    assert to_text_resp.status_code == 200, to_text_resp.text
    body = to_text_resp.json()
    assert body["text"] == text
    assert body["binary"] == binary


async def test_binary_to_text_emoji_is_four_utf8_bytes(client, api_key):
    """Explicit byte-count/content check (not just "200 OK") for a
    multi-byte emoji, per the task brief."""
    resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": "🎉"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text
    groups = resp.json()["binary"].split()
    assert len(groups) == 4
    assert groups == [format(b, "08b") for b in "🎉".encode("utf-8")]


@pytest.mark.parametrize(
    "binary",
    [
        "0100100",  # 7 chars - too short
        "010010000",  # 9 chars - too long
        "0000200a",  # 8 chars but contains a non-0/1 digit
        "0100a000",  # 8 chars but contains a letter
        "01001000 0110110",  # first group valid, second group too short
    ],
)
async def test_binary_to_text_malformed_group_returns_400_invalid_binary_format(
    client, api_key, binary
):
    resp = await client.post(
        "/v1/binary_tools/binary_to_text",
        json={"binary": binary},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_BINARY_FORMAT"


@pytest.mark.parametrize(
    "binary",
    [
        "10000000",  # a lone UTF-8 continuation byte (0x80), no lead byte
        "11110000",  # a truncated 4-byte lead byte (0xF0) with no continuation bytes
        "11000000 00000001",  # overlong/invalid 2-byte sequence
    ],
)
async def test_binary_to_text_invalid_utf8_sequence_returns_400(client, api_key, binary):
    resp = await client.post(
        "/v1/binary_tools/binary_to_text",
        json={"binary": binary},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 400, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["error"]["code"] == "INVALID_UTF8_SEQUENCE"


async def test_binary_to_text_empty_binary_returns_422(client, api_key):
    """`binary` now carries `min_length=1` (matching `app/models/text.py`'s
    convention), so an empty string is rejected by Pydantic before the
    handler's manual empty-check (removed as dead code) would ever run."""
    resp = await client.post(
        "/v1/binary_tools/binary_to_text",
        json={"binary": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_text_to_binary_empty_text_returns_422(client, api_key):
    """Same convention as `binary_to_text`: `text` carries `min_length=1`,
    so Pydantic rejects an empty string with a 422 before the handler runs."""
    resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": ""},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_text_to_binary_text_over_max_length_returns_422(client, api_key):
    resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": "a" * 10001},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_text_to_binary_text_at_max_length_returns_200(client, api_key):
    resp = await client.post(
        "/v1/binary_tools/text_to_binary",
        json={"text": "a" * 10000},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 200, resp.text


async def test_binary_to_text_binary_over_max_length_returns_422(client, api_key):
    # 100001 chars exceeds the 100000-char bound; content doesn't matter
    # since the length check happens before any group validation.
    resp = await client.post(
        "/v1/binary_tools/binary_to_text",
        json={"binary": "0" * 100001},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 422, resp.text


async def test_binary_to_text_missing_binary_field_returns_400(client, api_key):
    resp = await client.post(
        "/v1/binary_tools/binary_to_text",
        json={},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code in (400, 422), resp.text


async def test_binary_to_text_generic_exception_returns_500_without_leaking_exception_text(
    client, api_key, monkeypatch
):
    monkeypatch.setattr(binary_tools_router, "bytes", _boom_bytes, raising=False)
    resp = await client.post(
        "/v1/binary_tools/binary_to_text",
        json={"binary": "01001000 01101001"},
        headers={"X-API-Key": api_key["key"]},
    )
    assert resp.status_code == 500, resp.text
    body = resp.json()
    assert body["success"] is False
    assert body["message"] == "Failed to convert binary to text"
    assert body["error"]["code"] == "BINARY_TO_TEXT_CONVERSION_FAILED"
    assert "SECRET_MARKER" not in resp.text
    assert "hunter2" not in resp.text
    assert "db_password" not in resp.text
    assert "RuntimeError" not in resp.text
    assert "binary_tools.py" not in resp.text
