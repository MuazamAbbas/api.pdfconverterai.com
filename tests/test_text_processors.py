"""Unit tests for `app.services.text.processors.TextParaphraseProcessor`/
`TextSummarizeProcessor` (Handbook Part C.4, ADR-003), mirroring
`tests/test_pdf_summarize_processor.py`'s pure-unit style: no Mongo/Redis, a
lightweight fake `file_doc` object (`SimpleNamespace` over a real small text
file on disk, since `_read_text_input` does a plain `open(...).read()`), and
the actual pipeline-calling function (`paraphrase_text`/
`summarize_text_service`) monkeypatched at its *definition* site
(`app.services.text.paraphrase.paraphrase_text` /
`app.services.text.summarize.summarize_text_service`) rather than a real
t5-small model - that's the worker-process-level, live concern covered
separately (see `tests/test_worker_startup_shutdown.py` and
`tests/test_worker_retry_text_web_tools.py`).

This is the direct unit test for the `ctx["paraphrase_pipeline"]`/
`ctx["summarize_pipeline"]` threading behavior: `execute()` must raise
`TransientProcessingError` (retryable) - never crash, never
`PermanentProcessingError` - whenever `ctx` has no matching pipeline key
(missing key, or `ctx` itself is `None`), since that only means the worker
process hasn't finished `on_startup()` yet.
"""
import os
from types import SimpleNamespace

import pytest

from app.services.jobs.processor import PermanentProcessingError, TransientProcessingError
from app.services.text.processors import TextParaphraseProcessor, TextSummarizeProcessor

# Same session-scoped loop as every other test module (see
# tests/test_worker_retry.py's comment) - kept for suite-wide consistency
# even though this module never touches Mongo/Redis itself.
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _fake_text_file_doc(tmp_path, filename: str, text: str = "Some input text.", write_file: bool = True):
    storage_path = str(tmp_path / filename)
    if write_file:
        with open(storage_path, "w", encoding="utf-8") as f:
            f.write(text)
    return SimpleNamespace(originalFilename=filename, storagePath=storage_path)


# --- TextParaphraseProcessor -------------------------------------------------


async def test_paraphrase_missing_source_file_fails_validation(tmp_path):
    file_doc = _fake_text_file_doc(tmp_path, "gone.txt", write_file=False)
    assert not os.path.exists(file_doc.storagePath)

    with pytest.raises(PermanentProcessingError, match="missing or has expired"):
        await TextParaphraseProcessor().run(
            job=object(), file_doc=file_doc, ctx={"paraphrase_pipeline": object()}
        )


@pytest.mark.parametrize("text", ["", "   ", "\n\t"])
async def test_paraphrase_rejects_empty_or_whitespace_text(tmp_path, text):
    file_doc = _fake_text_file_doc(tmp_path, "empty.txt", text=text)

    with pytest.raises(PermanentProcessingError, match="non-empty string"):
        await TextParaphraseProcessor().run(
            job=object(), file_doc=file_doc, ctx={"paraphrase_pipeline": object()}
        )


async def test_paraphrase_execute_raises_transient_when_ctx_is_none(tmp_path, monkeypatch):
    file_doc = _fake_text_file_doc(tmp_path, "hello.txt", text="Hello there, this is some text.")

    calls = {"n": 0}

    async def _should_not_be_called(text, pipeline):
        calls["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.services.text.paraphrase.paraphrase_text", _should_not_be_called)

    with pytest.raises(TransientProcessingError, match="not loaded yet"):
        await TextParaphraseProcessor().run(job=object(), file_doc=file_doc, ctx=None)
    assert calls["n"] == 0


async def test_paraphrase_execute_raises_transient_when_pipeline_missing_from_ctx(tmp_path, monkeypatch):
    file_doc = _fake_text_file_doc(tmp_path, "hello.txt", text="Hello there, this is some text.")

    calls = {"n": 0}

    async def _should_not_be_called(text, pipeline):
        calls["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.services.text.paraphrase.paraphrase_text", _should_not_be_called)

    with pytest.raises(TransientProcessingError, match="not loaded yet"):
        await TextParaphraseProcessor().run(job=object(), file_doc=file_doc, ctx={})
    assert calls["n"] == 0


async def test_paraphrase_execute_success_passes_ctx_pipeline_through(tmp_path, monkeypatch):
    file_doc = _fake_text_file_doc(tmp_path, "hello.txt", text="Hello there, this is some text.")
    sentinel_pipeline = object()
    received = {}

    async def _fake_paraphrase(text, pipeline):
        received["text"] = text
        received["pipeline"] = pipeline
        return "A paraphrase of the text."

    monkeypatch.setattr("app.services.text.paraphrase.paraphrase_text", _fake_paraphrase)

    result = await TextParaphraseProcessor().run(
        job=object(), file_doc=file_doc, ctx={"paraphrase_pipeline": sentinel_pipeline}
    )

    assert result == {"paraphrased": "A paraphrase of the text."}
    assert received["pipeline"] is sentinel_pipeline
    assert received["text"] == "Hello there, this is some text."


async def test_paraphrase_permanent_error_is_not_retried(tmp_path, monkeypatch):
    """`execute()` deliberately raises a fixed, hardcoded client-facing
    message here rather than passing the underlying `ValueError`'s text
    through verbatim (issue #39 - Batch 5 of the #27/#29/#31 error-leak
    class) - it happens to be textually identical to `paraphrase_text`'s
    own documented message today, but this asserts the exact literal (and
    the `from e` chain / `__cause__`) rather than relying on that
    coincidence, since the whole point of the fix is decoupling the two."""
    file_doc = _fake_text_file_doc(tmp_path, "hello.txt", text="Hello there, this is some text.")

    original = ValueError("Text must be a non-empty string")

    async def _fake_paraphrase(text, pipeline):
        raise original

    monkeypatch.setattr("app.services.text.paraphrase.paraphrase_text", _fake_paraphrase)

    with pytest.raises(PermanentProcessingError) as exc_info:
        await TextParaphraseProcessor().run(
            job=object(), file_doc=file_doc, ctx={"paraphrase_pipeline": object()}
        )

    assert str(exc_info.value) == "Text must be a non-empty string"
    assert exc_info.value.__cause__ is original


async def test_paraphrase_unexpected_error_is_wrapped_as_transient(tmp_path, monkeypatch):
    file_doc = _fake_text_file_doc(tmp_path, "hello.txt", text="Hello there, this is some text.")

    async def _fake_paraphrase(text, pipeline):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.text.paraphrase.paraphrase_text", _fake_paraphrase)

    with pytest.raises(TransientProcessingError):
        await TextParaphraseProcessor().run(
            job=object(), file_doc=file_doc, ctx={"paraphrase_pipeline": object()}
        )


async def test_paraphrase_verify_rejects_empty_result_directly():
    with pytest.raises(PermanentProcessingError, match="no output"):
        await TextParaphraseProcessor().verify(job=object(), file_doc=object(), result={"paraphrased": ""})


# --- TextSummarizeProcessor ---------------------------------------------------


_LONG_ENOUGH_TEXT = "This is a sufficiently long piece of text for summarization to be attempted on it."
assert len(_LONG_ENOUGH_TEXT) >= 50


async def test_summarize_missing_source_file_fails_validation(tmp_path):
    file_doc = _fake_text_file_doc(tmp_path, "gone.txt", write_file=False)

    with pytest.raises(PermanentProcessingError, match="missing or has expired"):
        await TextSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarize_pipeline": object()}
        )


async def test_summarize_rejects_text_under_fifty_characters(tmp_path):
    file_doc = _fake_text_file_doc(tmp_path, "short.txt", text="too short")

    with pytest.raises(PermanentProcessingError, match="at least 50 characters"):
        await TextSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarize_pipeline": object()}
        )


async def test_summarize_execute_raises_transient_when_ctx_is_none(tmp_path, monkeypatch):
    file_doc = _fake_text_file_doc(tmp_path, "long.txt", text=_LONG_ENOUGH_TEXT)

    calls = {"n": 0}

    async def _should_not_be_called(text, pipeline):
        calls["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.services.text.summarize.summarize_text_service", _should_not_be_called)

    with pytest.raises(TransientProcessingError, match="not loaded yet"):
        await TextSummarizeProcessor().run(job=object(), file_doc=file_doc, ctx=None)
    assert calls["n"] == 0


async def test_summarize_execute_raises_transient_when_pipeline_missing_from_ctx(tmp_path, monkeypatch):
    file_doc = _fake_text_file_doc(tmp_path, "long.txt", text=_LONG_ENOUGH_TEXT)

    calls = {"n": 0}

    async def _should_not_be_called(text, pipeline):
        calls["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.services.text.summarize.summarize_text_service", _should_not_be_called)

    with pytest.raises(TransientProcessingError, match="not loaded yet"):
        await TextSummarizeProcessor().run(job=object(), file_doc=file_doc, ctx={})
    assert calls["n"] == 0


async def test_summarize_execute_success_passes_ctx_pipeline_through(tmp_path, monkeypatch):
    file_doc = _fake_text_file_doc(tmp_path, "long.txt", text=_LONG_ENOUGH_TEXT)
    sentinel_pipeline = object()
    received = {}

    async def _fake_summarize(text, pipeline):
        received["text"] = text
        received["pipeline"] = pipeline
        return "A short summary."

    monkeypatch.setattr("app.services.text.summarize.summarize_text_service", _fake_summarize)

    result = await TextSummarizeProcessor().run(
        job=object(), file_doc=file_doc, ctx={"summarize_pipeline": sentinel_pipeline}
    )

    assert result == {"summary": "A short summary."}
    assert received["pipeline"] is sentinel_pipeline
    assert received["text"] == _LONG_ENOUGH_TEXT


async def test_summarize_permanent_error_is_not_retried(tmp_path, monkeypatch):
    """`execute()` deliberately raises a fixed, hardcoded client-facing
    message here rather than passing the underlying `ValueError`'s text
    through verbatim (issue #39 - Batch 5 of the #27/#29/#31 error-leak
    class) - it happens to be textually identical to
    `summarize_text_service`'s own documented message today, but this
    asserts the exact literal (and the `from e` chain / `__cause__`) rather
    than relying on that coincidence, since the whole point of the fix is
    decoupling the two."""
    file_doc = _fake_text_file_doc(tmp_path, "long.txt", text=_LONG_ENOUGH_TEXT)

    original = ValueError("Text must be at least 50 characters")

    async def _fake_summarize(text, pipeline):
        raise original

    monkeypatch.setattr("app.services.text.summarize.summarize_text_service", _fake_summarize)

    with pytest.raises(PermanentProcessingError) as exc_info:
        await TextSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarize_pipeline": object()}
        )

    assert str(exc_info.value) == "Text must be at least 50 characters"
    assert exc_info.value.__cause__ is original


async def test_summarize_unexpected_error_is_wrapped_as_transient(tmp_path, monkeypatch):
    file_doc = _fake_text_file_doc(tmp_path, "long.txt", text=_LONG_ENOUGH_TEXT)

    async def _fake_summarize(text, pipeline):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.text.summarize.summarize_text_service", _fake_summarize)

    with pytest.raises(TransientProcessingError):
        await TextSummarizeProcessor().run(
            job=object(), file_doc=file_doc, ctx={"summarize_pipeline": object()}
        )


async def test_summarize_verify_rejects_empty_result_directly():
    with pytest.raises(PermanentProcessingError, match="no output"):
        await TextSummarizeProcessor().verify(job=object(), file_doc=object(), result={"summary": ""})
