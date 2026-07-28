"""Unit tests for `app.services.pdf.processors.PdfSummarizeProcessor`
(Handbook Part C.4, ADR-003; ADR-015 Open Item 2 - model-loading migration),
mirroring `tests/test_image_processor.py`'s pure-unit style: no Mongo/Redis,
a lightweight fake `file_doc` object, and `summarize_pdf_service` (imported
lazily inside `execute()` from `app.services.pdf.summarize`, see that
module's docstring for why) monkeypatched at its *definition* site
(`app.services.pdf.summarize.summarize_pdf_service`) rather than the real
`facebook/bart-large-cnn` model - that's the worker-process-level, live
concern covered separately (see `tests/test_worker_startup_shutdown.py`).

This is the direct unit test for the new `ctx["summarizer_pipeline"]`
threading behavior called out in the migration brief: `execute()` must raise
`TransientProcessingError` (retryable) - never crash, never
`PermanentProcessingError` - whenever `ctx` has no `summarizer_pipeline`
(missing key, or `ctx` itself is `None`), since that only means the worker
process hasn't finished `on_startup()` yet.
"""
import os
from types import SimpleNamespace

import pytest

from app.services.jobs.processor import PermanentProcessingError, TransientProcessingError
from app.services.pdf.processors import PdfSummarizeProcessor

# Same session-scoped loop as every other test module (see
# tests/test_worker_retry.py's comment) - kept for suite-wide consistency
# even though this module never touches Mongo/Redis itself.
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _fake_pdf_file_doc(tmp_path, filename: str, content: bytes = b"%PDF-1.4 fake but present\n%%EOF", write_file: bool = True):
    storage_path = str(tmp_path / filename)
    if write_file:
        with open(storage_path, "wb") as f:
            f.write(content)
    return SimpleNamespace(originalFilename=filename, storagePath=storage_path)


async def test_execute_raises_transient_when_ctx_is_none(tmp_path, monkeypatch):
    file_doc = _fake_pdf_file_doc(tmp_path, "report.pdf")

    calls = {"n": 0}

    async def _should_not_be_called(path, summarizer):
        calls["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.services.pdf.summarize.summarize_pdf_service", _should_not_be_called)

    with pytest.raises(TransientProcessingError, match="not loaded yet"):
        await PdfSummarizeProcessor().run(job=object(), file_doc=file_doc, ctx=None)
    assert calls["n"] == 0, "execute() must never call the summarizer when no pipeline is available"


async def test_execute_raises_transient_when_pipeline_missing_from_ctx(tmp_path, monkeypatch):
    """`ctx` present but without the `summarizer_pipeline` key (e.g. the
    worker process is mid-startup, or a caller forgot to thread it) - same
    transient/retryable classification as `ctx=None`."""
    file_doc = _fake_pdf_file_doc(tmp_path, "report.pdf")

    calls = {"n": 0}

    async def _should_not_be_called(path, summarizer):
        calls["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.services.pdf.summarize.summarize_pdf_service", _should_not_be_called)

    with pytest.raises(TransientProcessingError, match="not loaded yet"):
        await PdfSummarizeProcessor().run(job=object(), file_doc=file_doc, ctx={})
    assert calls["n"] == 0


async def test_execute_success_passes_ctx_pipeline_through_and_returns_summary(tmp_path, monkeypatch):
    file_doc = _fake_pdf_file_doc(tmp_path, "report.pdf")
    sentinel_pipeline = object()
    received = {}

    async def _fake_summarize(path, summarizer):
        received["path"] = path
        received["summarizer"] = summarizer
        return "A short summary."

    monkeypatch.setattr("app.services.pdf.summarize.summarize_pdf_service", _fake_summarize)

    result = await PdfSummarizeProcessor().run(
        job=object(), file_doc=file_doc, ctx={"summarizer_pipeline": sentinel_pipeline}
    )

    assert result == {"summary": "A short summary."}
    assert received["summarizer"] is sentinel_pipeline, (
        "execute() must pass ctx['summarizer_pipeline'] straight through to "
        "summarize_pdf_service, not load/instantiate its own"
    )
    assert received["path"] == file_doc.storagePath


async def test_permanent_error_from_summarize_service_is_not_retried(tmp_path, monkeypatch):
    """`summarize_pdf_service` raises `ValueError` for corrupted/too-short
    text (per its own docstring) - `execute()` must convert this to
    `PermanentProcessingError`, never `TransientProcessingError`."""
    file_doc = _fake_pdf_file_doc(tmp_path, "report.pdf")

    async def _fake_summarize(path, summarizer):
        raise ValueError("Text must be at least 50 characters")

    monkeypatch.setattr("app.services.pdf.summarize.summarize_pdf_service", _fake_summarize)

    with pytest.raises(PermanentProcessingError, match="at least 50 characters"):
        await PdfSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarizer_pipeline": object()}
        )


async def test_unexpected_error_from_summarize_service_is_wrapped_as_transient(tmp_path, monkeypatch):
    file_doc = _fake_pdf_file_doc(tmp_path, "report.pdf")

    async def _fake_summarize(path, summarizer):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.pdf.summarize.summarize_pdf_service", _fake_summarize)

    with pytest.raises(TransientProcessingError):
        await PdfSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarizer_pipeline": object()}
        )


async def test_verify_rejects_empty_summary_result_directly():
    with pytest.raises(PermanentProcessingError, match="no output"):
        await PdfSummarizeProcessor().verify(job=object(), file_doc=object(), result={"summary": ""})


async def test_missing_source_file_fails_validation_before_execute(tmp_path, monkeypatch):
    """Validation must run (and reject) before `execute()` ever looks at
    `ctx` - a missing file is a permanent failure regardless of whether the
    summarizer pipeline is loaded."""
    file_doc = _fake_pdf_file_doc(tmp_path, "gone.pdf", write_file=False)
    assert not os.path.exists(file_doc.storagePath)

    calls = {"n": 0}

    async def _should_not_be_called(path, summarizer):
        calls["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.services.pdf.summarize.summarize_pdf_service", _should_not_be_called)

    with pytest.raises(PermanentProcessingError, match="missing or has expired"):
        await PdfSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarizer_pipeline": object()}
        )
    assert calls["n"] == 0


async def test_non_pdf_extension_fails_validation(tmp_path):
    file_doc = _fake_pdf_file_doc(tmp_path, "notes.txt")

    with pytest.raises(PermanentProcessingError, match="must be a PDF"):
        await PdfSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarizer_pipeline": object()}
        )
