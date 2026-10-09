"""Shared capped, chunked body-read helper (Handbook Part C.10).

`aiohttp.StreamReader.read(n)` is NOT "read n bytes or EOF" - it returns as
soon as *any* data is in the buffer, which can be far fewer than `n` bytes
for a chunked-transfer-encoded or TCP-segmented response (i.e. almost every
real webpage). A single `read(max_bytes + 1)` call therefore often
under-reads real pages instead of reading up to the cap, rather than
throwing an error - the bug is invisible unless specifically tested with a
response that arrives in multiple small reads.

`read_capped_body()` loops fixed-size reads until EOF (`b""`) or until the
accumulated total exceeds `max_bytes`, at which point it stops immediately
rather than draining the rest of an (potentially huge/malicious) oversized
stream.

Originally written inline in `app/services/web_tools/summarize.py` (closing
`api.pdfconverterai.com#98`); extracted here once `app/services/seo/
seo_audit.py::_fetch_main_page()` needed the identical fix
(`api.pdfconverterai.com#99`) so both callers share one implementation.
"""
import aiohttp

_DEFAULT_CHUNK_SIZE = 65_536


async def read_capped_body(
    response: aiohttp.ClientResponse,
    max_bytes: int,
    chunk_size: int = _DEFAULT_CHUNK_SIZE,
) -> tuple[bytes, bool]:
    """Reads `response`'s body in fixed `chunk_size` chunks until EOF or
    until the accumulated total exceeds `max_bytes`, whichever comes first.

    Returns `(body, truncated)`: `body` is capped at `max_bytes`;
    `truncated` is True only when more than `max_bytes` was actually
    available on the stream (so a response that lands exactly at the cap
    is correctly reported as not truncated).
    """
    chunks: list[bytes] = []
    total = 0
    while total <= max_bytes:
        chunk = await response.content.read(chunk_size)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)

    body = b"".join(chunks)
    truncated = total > max_bytes
    return body[:max_bytes], truncated
