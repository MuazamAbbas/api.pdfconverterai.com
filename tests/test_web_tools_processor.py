"""Unit tests for `app.services.web_tools.processors.WebToolsSummarizeProcessor`
(Handbook Part C.4, ADR-003), mirroring `tests/test_pdf_summarize_processor.py`'s
pure-unit style: no Mongo/Redis, a lightweight fake `file_doc` object over a
real small text file on disk (holding the URL string, since `_read_url_input`
does a plain `open(...).read().strip()`).

No real network call is ever made: `fetch_webpage_text`/`summarize_webpage_text`
(imported lazily inside `execute()` from `app.services.web_tools.summarize`)
are monkeypatched at their *definition* site, the same idiom
`tests/test_pdf_summarize_processor.py` uses for `summarize_pdf_service` and
`tests/test_image_processor.py` uses for `extract_text_from_image` - i.e.
`aiohttp` itself is never touched here; the module-level function that wraps
it is swapped out instead, per the task brief.
"""
import os
from types import SimpleNamespace

import aiohttp
import pytest

from app.services.jobs.processor import PermanentProcessingError, TransientProcessingError
from app.services.web_tools.processors import WebToolsSummarizeProcessor

# Same session-scoped loop as every other test module (see
# tests/test_worker_retry.py's comment) - kept for suite-wide consistency
# even though this module never touches Mongo/Redis itself.
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _fake_url_file_doc(tmp_path, filename: str, url: str = "https://example.com", write_file: bool = True):
    storage_path = str(tmp_path / filename)
    if write_file:
        with open(storage_path, "w", encoding="utf-8") as f:
            f.write(url)
    return SimpleNamespace(originalFilename=filename, storagePath=storage_path)


async def test_missing_source_file_fails_validation(tmp_path):
    file_doc = _fake_url_file_doc(tmp_path, "gone.txt", write_file=False)
    assert not os.path.exists(file_doc.storagePath)

    with pytest.raises(PermanentProcessingError, match="missing or has expired"):
        await WebToolsSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarize_pipeline": object()}
        )


@pytest.mark.parametrize("bad_url", ["ftp://example.com", "example.com", "not a url", ""])
async def test_validate_rejects_non_http_prefixed_url(tmp_path, bad_url):
    file_doc = _fake_url_file_doc(tmp_path, "bad.txt", url=bad_url)

    with pytest.raises(PermanentProcessingError, match="http:// or https://"):
        await WebToolsSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarize_pipeline": object()}
        )


async def test_execute_raises_transient_when_pipeline_missing_but_fetch_succeeds(tmp_path, monkeypatch):
    """The page fetch happens before the pipeline is checked (see
    `execute()`'s ordering) - a successful fetch with no `summarize_pipeline`
    in `ctx` must still be treated as transient/retryable, never crash and
    never call the (unavailable) pipeline."""
    file_doc = _fake_url_file_doc(tmp_path, "url.txt")

    async def _fake_fetch(url):
        return "Some fetched webpage text, long enough to summarize." * 2

    calls = {"n": 0}

    async def _should_not_be_called(text, pipeline):
        calls["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)
    monkeypatch.setattr("app.services.web_tools.summarize.summarize_webpage_text", _should_not_be_called)

    with pytest.raises(TransientProcessingError, match="not loaded yet"):
        await WebToolsSummarizeProcessor().run(job=object(), file_doc=file_doc, ctx=None)
    assert calls["n"] == 0

    with pytest.raises(TransientProcessingError, match="not loaded yet"):
        await WebToolsSummarizeProcessor().run(job=object(), file_doc=file_doc, ctx={})
    assert calls["n"] == 0


async def test_fetch_network_error_is_transient(tmp_path, monkeypatch):
    file_doc = _fake_url_file_doc(tmp_path, "url.txt")

    async def _fake_fetch(url):
        raise aiohttp.ClientConnectionError("simulated connection failure")

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)

    with pytest.raises(TransientProcessingError, match="fetching the webpage"):
        await WebToolsSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarize_pipeline": object()}
        )


async def test_fetch_no_text_extracted_is_permanent(tmp_path, monkeypatch):
    """`execute()` deliberately raises a fixed, hardcoded client-facing
    message here rather than passing the underlying `ValueError`'s text
    through verbatim (issue #39 - Batch 5 of the #27/#29/#31 error-leak
    class), so this asserts the exact literal - not just a passthrough of
    whatever `fetch_webpage_text` happened to raise - and that the `from e`
    chain (`__cause__`) is preserved for server-side traceback visibility."""
    file_doc = _fake_url_file_doc(tmp_path, "url.txt")

    original = ValueError("No text extracted from webpage")

    async def _fake_fetch(url):
        raise original

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)

    with pytest.raises(PermanentProcessingError) as exc_info:
        await WebToolsSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarize_pipeline": object()}
        )

    assert str(exc_info.value) == "No text extracted from the webpage, or it is too short"
    assert exc_info.value.__cause__ is original


async def test_execute_success_passes_fetched_text_and_ctx_pipeline_through(tmp_path, monkeypatch):
    file_doc = _fake_url_file_doc(tmp_path, "url.txt", url="https://example.com/article")
    sentinel_pipeline = object()
    received = {}

    async def _fake_fetch(url):
        received["url"] = url
        return "Fetched webpage paragraph text."

    async def _fake_summarize(text, pipeline):
        received["text"] = text
        received["pipeline"] = pipeline
        return "A short webpage summary."

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)
    monkeypatch.setattr("app.services.web_tools.summarize.summarize_webpage_text", _fake_summarize)

    result = await WebToolsSummarizeProcessor().run(
        job=object(), file_doc=file_doc, ctx={"summarize_pipeline": sentinel_pipeline}
    )

    assert result == {"summary": "A short webpage summary."}
    assert received["url"] == "https://example.com/article"
    assert received["text"] == "Fetched webpage paragraph text."
    assert received["pipeline"] is sentinel_pipeline


async def test_summarize_step_unexpected_error_is_wrapped_as_transient(tmp_path, monkeypatch):
    file_doc = _fake_url_file_doc(tmp_path, "url.txt")

    async def _fake_fetch(url):
        return "Fetched webpage paragraph text."

    async def _fake_summarize(text, pipeline):
        raise RuntimeError("simulated model failure")

    monkeypatch.setattr("app.services.web_tools.summarize.fetch_webpage_text", _fake_fetch)
    monkeypatch.setattr("app.services.web_tools.summarize.summarize_webpage_text", _fake_summarize)

    with pytest.raises(TransientProcessingError, match="summarizing the webpage"):
        await WebToolsSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarize_pipeline": object()}
        )


async def test_verify_rejects_empty_summary_result_directly():
    with pytest.raises(PermanentProcessingError, match="no output"):
        await WebToolsSummarizeProcessor().verify(job=object(), file_doc=object(), result={"summary": ""})
