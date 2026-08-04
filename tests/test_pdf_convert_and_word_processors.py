"""Unit tests for `app.services.pdf.processors.PdfConvertProcessor` /
`PdfToWordProcessor`'s `execute()` `except ValueError` -> `PermanentProcessingError`
mapping (Handbook Part C.4, ADR-003), mirroring
`tests/test_pdf_summarize_processor.py` / `tests/test_image_processor.py`'s
pure-unit style: no Mongo/Redis, a lightweight fake `file_doc` object, and
the underlying service function (`extract_text_from_pdf` /
`convert_pdf_to_word`) monkeypatched at its `app.services.pdf.processors`
import site rather than invoking real PyMuPDF/pdf2docx.

These two Processors' permanent-failure paths were previously untested at
this granularity: `tests/test_worker_retry.py` covers `PdfConvertProcessor`
only via a real corrupt-PDF fixture through the full worker/Mongo stack
(asserting only that `job.error` is set and doesn't leak a traceback, not
the exact message), and `PdfToWordProcessor` had no dedicated test at all.
Both Processors' `except ValueError` branches now raise a fixed, hardcoded
client-facing message rather than passing the underlying `ValueError`'s
text through verbatim (issue #39 - Batch 5 of the #27/#29/#31 error-leak
class) - these tests assert the exact literal and that the `from e` chain
(`__cause__`) is preserved for server-side traceback visibility.
"""
from types import SimpleNamespace

import pytest

from app.services.jobs.processor import PermanentProcessingError, TransientProcessingError
from app.services.pdf.processors import PdfConvertProcessor, PdfToWordProcessor

# Same session-scoped loop as every other test module (see
# tests/test_worker_retry.py's comment) - kept for suite-wide consistency
# even though this module never touches Mongo/Redis itself.
pytestmark = pytest.mark.asyncio(loop_scope="session")


def _fake_pdf_file_doc(tmp_path, filename: str, content: bytes = b"%PDF-1.4 fake but present\n%%EOF"):
    storage_path = str(tmp_path / filename)
    with open(storage_path, "wb") as f:
        f.write(content)
    return SimpleNamespace(originalFilename=filename, storagePath=storage_path)


# --- PdfConvertProcessor -----------------------------------------------------


async def test_convert_value_error_raises_permanent_error_with_hardcoded_message(tmp_path, monkeypatch):
    file_doc = _fake_pdf_file_doc(tmp_path, "corrupt.pdf")

    original = ValueError("Invalid or unreadable PDF file")

    async def _fake_extract(path):
        raise original

    monkeypatch.setattr("app.services.pdf.processors.extract_text_from_pdf", _fake_extract)

    with pytest.raises(PermanentProcessingError) as exc_info:
        await PdfConvertProcessor().run(job=object(), file_doc=file_doc)

    assert str(exc_info.value) == "Invalid or unreadable PDF file, or no text could be extracted"
    assert exc_info.value.__cause__ is original


async def test_convert_unexpected_os_error_is_wrapped_as_transient(tmp_path, monkeypatch):
    file_doc = _fake_pdf_file_doc(tmp_path, "hello.pdf")

    async def _fake_extract(path):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr("app.services.pdf.processors.extract_text_from_pdf", _fake_extract)

    with pytest.raises(TransientProcessingError):
        await PdfConvertProcessor().run(job=object(), file_doc=file_doc)


# --- PdfToWordProcessor -------------------------------------------------------


async def test_to_word_value_error_raises_permanent_error_with_hardcoded_message(tmp_path, monkeypatch):
    file_doc = _fake_pdf_file_doc(tmp_path, "corrupt.pdf")

    original = ValueError("Source PDF not found")

    async def _fake_convert(pdf_path, output_dir):
        raise original

    monkeypatch.setattr("app.services.pdf.pdf_to_word.convert_pdf_to_word", _fake_convert)

    with pytest.raises(PermanentProcessingError) as exc_info:
        await PdfToWordProcessor().run(job=object(), file_doc=file_doc)

    assert str(exc_info.value) == "Source PDF file is missing or has expired"
    assert exc_info.value.__cause__ is original


async def test_to_word_unexpected_error_is_wrapped_as_transient(tmp_path, monkeypatch):
    file_doc = _fake_pdf_file_doc(tmp_path, "hello.pdf")

    async def _fake_convert(pdf_path, output_dir):
        raise RuntimeError("simulated pdf2docx failure")

    monkeypatch.setattr("app.services.pdf.pdf_to_word.convert_pdf_to_word", _fake_convert)

    with pytest.raises(TransientProcessingError):
        await PdfToWordProcessor().run(job=object(), file_doc=file_doc)
