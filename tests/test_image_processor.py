"""Unit tests for `app.services.image.processors.ImageOcrProcessor`
(Handbook Part C.4, ADR-003), mirroring `tests/test_processor_base.py`'s
pure-unit style: no Mongo/Redis, `extract_text_from_image` is monkeypatched
at its `app.services.image.processors` import site rather than invoking the
real Tesseract binary.

This is deliberately at the same level as `tests/test_processor_base.py`
(no Mongo `files`/`jobs` documents - a lightweight fake `file_doc` object is
enough, since `ImageOcrProcessor` never touches `job` and only reads
`file_doc.originalFilename`/`file_doc.storagePath`), rather than
`tests/test_worker_retry.py`'s real-Mongo style - see
`tests/test_worker_retry_image.py` for the worker-orchestration-level
(Mongo-backed) retry tests that build on top of this.

Real Tesseract is intentionally not exercised here (see
`tests/test_files_jobs_image_flow.py::_tesseract_available`'s docstring for
why it isn't reachable on this checkout at all) - `extract_text_from_image`
itself is `app/services/image/ocr.py`'s responsibility, not
`ImageOcrProcessor`'s; this module only tests the processor's own
Validate/Prepare/Execute/Verify contract against that function's documented
behavior (returns non-empty text, or raises `ValueError` when no text is
detected, or raises some other `Exception` for anything else going wrong).
"""
import os
from types import SimpleNamespace

import pytest

from app.services.image.processors import ImageOcrProcessor
from app.services.jobs.processor import PermanentProcessingError, TransientProcessingError

# Same session-scoped loop as every other test module (see
# tests/test_worker_retry.py's comment) - kept for suite-wide consistency
# even though this module never touches Mongo/Redis itself.
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _fake_file_doc(tmp_path, filename: str, write_file: bool = True):
    storage_path = str(tmp_path / filename)
    if write_file:
        with open(storage_path, "wb") as f:
            f.write(b"not-real-image-bytes-just-a-placeholder")
    return SimpleNamespace(originalFilename=filename, storagePath=storage_path)


async def test_execute_success_returns_extracted_text(tmp_path, monkeypatch):
    file_doc = _fake_file_doc(tmp_path, "hello.png")

    async def _fake_extract(image_data: bytes) -> str:
        return "HELLO WORLD"

    monkeypatch.setattr("app.services.image.processors.extract_text_from_image", _fake_extract)

    result = await ImageOcrProcessor().run(job=object(), file_doc=file_doc)
    assert result == {"text": "HELLO WORLD"}


async def test_no_text_detected_raises_permanent_error_no_retry(tmp_path, monkeypatch):
    """Mirrors `app/services/image/ocr.py::extract_text_from_image`'s real
    behavior for a blank/textless image: it raises `ValueError("No text
    detected in image")`, which `ImageOcrProcessor.execute` must convert to
    a `PermanentProcessingError` (never a `TransientProcessingError`) -
    ADR-003's retry classification."""
    file_doc = _fake_file_doc(tmp_path, "blank.png")

    async def _fake_extract(image_data: bytes) -> str:
        raise ValueError("No text detected in image")

    monkeypatch.setattr("app.services.image.processors.extract_text_from_image", _fake_extract)

    with pytest.raises(PermanentProcessingError):
        await ImageOcrProcessor().run(job=object(), file_doc=file_doc)


async def test_verify_rejects_empty_text_result_directly():
    """Defense-in-depth: even if `execute()` somehow returned an empty
    string without raising (not how the real `extract_text_from_image`
    behaves today, but `verify()` guards against it independently, matching
    `PdfConvertProcessor.verify`'s identical empty-result check), `verify()`
    on its own must still reject it."""
    with pytest.raises(PermanentProcessingError, match="No text could be extracted"):
        await ImageOcrProcessor().verify(job=object(), file_doc=object(), result={"text": ""})


async def test_execute_wraps_unexpected_error_as_transient(tmp_path, monkeypatch):
    file_doc = _fake_file_doc(tmp_path, "hello.png")

    async def _fake_extract(image_data: bytes) -> str:
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.image.processors.extract_text_from_image", _fake_extract)

    with pytest.raises(TransientProcessingError):
        await ImageOcrProcessor().run(job=object(), file_doc=file_doc)


async def test_missing_source_file_fails_validation_before_execute(tmp_path, monkeypatch):
    file_doc = _fake_file_doc(tmp_path, "gone.png", write_file=False)
    assert not os.path.exists(file_doc.storagePath)

    calls = {"n": 0}

    async def _fake_extract(image_data: bytes) -> str:
        calls["n"] += 1
        return "should not be reached"

    monkeypatch.setattr("app.services.image.processors.extract_text_from_image", _fake_extract)

    with pytest.raises(PermanentProcessingError, match="missing or has expired"):
        await ImageOcrProcessor().run(job=object(), file_doc=file_doc)
    assert calls["n"] == 0, "execute() must never run once validate() has already rejected the file"


async def test_unsupported_extension_fails_validation(tmp_path):
    file_doc = _fake_file_doc(tmp_path, "photo.gif")

    with pytest.raises(PermanentProcessingError, match="supported image"):
        await ImageOcrProcessor().run(job=object(), file_doc=file_doc)
