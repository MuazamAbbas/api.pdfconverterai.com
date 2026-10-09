"""Unit coverage for `app/shared/web/body_read.py::read_capped_body()`
(Handbook Part C.10), extracted from `app/services/web_tools/summarize.py`
(`#98`) once `app/services/seo/seo_audit.py` needed the identical fix
(`api.pdfconverterai.com#99`).

Exercised directly against a minimal fake response here (lower-level than
the per-caller integration coverage in `tests/test_web_tools_summarize_ssrf.py`
and `tests/test_seo_audit_service.py`, which prove each caller is actually
wired to this helper).
"""
import pytest

from app.shared.web.body_read import read_capped_body

pytestmark = pytest.mark.asyncio(loop_scope="session")


class _FakeStreamResponse:
    def __init__(self, body: bytes, max_read_chunk: int | None = None):
        self._body = body
        self._pos = 0
        # None = serve up to the requested `n` per call (still EOF-correct,
        # via the real cursor below) - a small int simulates a real
        # chunked-transfer-encoded/TCP-segmented remote that hands back far
        # fewer bytes than requested per read.
        self._max_read_chunk = max_read_chunk
        self.read_call_count = 0
        self.content = self

    async def read(self, n: int) -> bytes:
        self.read_call_count += 1
        limit = n if self._max_read_chunk is None else min(n, self._max_read_chunk)
        chunk = self._body[self._pos : self._pos + limit]
        self._pos += len(chunk)
        return chunk


async def test_read_capped_body_single_read_under_cap_not_truncated():
    response = _FakeStreamResponse(b"hello world")
    body, truncated = await read_capped_body(response, max_bytes=1_000)
    assert body == b"hello world"
    assert truncated is False


async def test_read_capped_body_assembles_full_body_from_many_small_chunks():
    """A single `read(n)` call returning far fewer than `n` bytes (real
    `StreamReader` behavior for a chunked/segmented response) must not
    under-read the body - this is the exact bug `#99` fixed."""
    body = b"a" * 10_000
    response = _FakeStreamResponse(body, max_read_chunk=7)

    result, truncated = await read_capped_body(response, max_bytes=20_000)

    assert result == body
    assert truncated is False
    # Proves multiple reads actually happened - a broken single-read
    # implementation would have returned only the first 7 bytes.
    assert response.read_call_count > len(body) // 7


async def test_read_capped_body_stops_once_over_cap_without_draining_whole_stream():
    """An over-cap body must be cut at `max_bytes` without draining the
    rest of the (potentially huge/malicious) remote stream - proven here by
    a stream twice the cap size, served in small chunks, with far fewer
    reads than a full drain would require."""
    chunk_size = 1_000
    cap = 5_000
    oversized_body = b"a" * (cap * 2)
    response = _FakeStreamResponse(oversized_body, max_read_chunk=chunk_size)

    result, truncated = await read_capped_body(response, max_bytes=cap, chunk_size=chunk_size)

    assert len(result) == cap
    assert truncated is True
    full_drain_call_count = len(oversized_body) // chunk_size
    assert response.read_call_count < full_drain_call_count


async def test_read_capped_body_landing_exactly_at_cap_is_not_truncated():
    cap = 100
    response = _FakeStreamResponse(b"a" * cap)

    result, truncated = await read_capped_body(response, max_bytes=cap)

    assert len(result) == cap
    assert truncated is False
