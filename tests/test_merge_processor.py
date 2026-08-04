"""Unit tests for `app.services.pdf.processors.MergeProcessor` (Handbook Part
C.4, ADR-003; Handbook Part I.2 - first multi-file Tier 2 tool).

Pure-unit style, mirroring `tests/test_pdf_convert_and_word_processors.py`/
`tests/test_pdf_summarize_processor.py`: no Mongo/Redis, lightweight fake
`file_doc` objects (`SimpleNamespace`), calling `validate`/`prepare`/
`execute`/`verify`/`cleanup` directly rather than via `Processor.run()` -
`MergeProcessor` deliberately isn't a `Processor` subclass (see
`app/services/pdf/processors.py`'s module docstring), so there's no `.run()`
to call here in the first place.

Real, distinguishable single-page PDFs are generated with PyMuPDF (`fitz`,
already a project dependency via `app/services/pdf/summarize.py` and used the
same way by `tests/test_worker_startup_shutdown.py`'s sibling fixtures) so the
merged output's page *order* can actually be verified by extracted text, not
just page count.
"""
import os
from types import SimpleNamespace

import PyPDF2
import pytest

from app.services.jobs.processor import PermanentProcessingError, TransientProcessingError
from app.services.pdf.processors import MergeProcessor

pytestmark = pytest.mark.asyncio(loop_scope="session")


def _make_marker_pdf(tmp_path, filename: str, marker_text: str) -> str:
    """Writes a single-page real PDF containing `marker_text`, via PyMuPDF."""
    import fitz

    path = str(tmp_path / filename)
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), marker_text)
    doc.save(path)
    doc.close()
    return path


def _fake_file_doc(storage_path: str, filename: str):
    return SimpleNamespace(originalFilename=filename, storagePath=storage_path)


async def test_validate_accepts_all_valid_pdf_file_docs(tmp_path):
    path_a = _make_marker_pdf(tmp_path, "a.pdf", "MARKER-A")
    path_b = _make_marker_pdf(tmp_path, "b.pdf", "MARKER-B")
    file_docs = [_fake_file_doc(path_a, "a.pdf"), _fake_file_doc(path_b, "b.pdf")]

    # Must not raise.
    await MergeProcessor().validate(job=object(), file_docs=file_docs)


async def test_validate_rejects_non_pdf_filename():
    file_docs = [SimpleNamespace(originalFilename="notes.txt", storagePath="/whatever")]

    with pytest.raises(PermanentProcessingError, match="File must be a PDF"):
        await MergeProcessor().validate(job=object(), file_docs=file_docs)


async def test_validate_rejects_missing_source_file(tmp_path):
    missing_path = str(tmp_path / "gone.pdf")
    file_docs = [_fake_file_doc(missing_path, "gone.pdf")]

    with pytest.raises(PermanentProcessingError, match="Source file is missing or has expired"):
        await MergeProcessor().validate(job=object(), file_docs=file_docs)


async def test_execute_merges_multiple_pdfs_in_input_order(tmp_path):
    """Output page order must match `file_docs` order, not e.g. filename
    sort order or any other incidental ordering."""
    path_b = _make_marker_pdf(tmp_path, "b.pdf", "MARKER-B")
    path_a = _make_marker_pdf(tmp_path, "a.pdf", "MARKER-A")
    path_c = _make_marker_pdf(tmp_path, "c.pdf", "MARKER-C")
    # Deliberately submitted out of filename-alphabetical order: B, A, C.
    file_docs = [
        _fake_file_doc(path_b, "b.pdf"),
        _fake_file_doc(path_a, "a.pdf"),
        _fake_file_doc(path_c, "c.pdf"),
    ]

    processor = MergeProcessor()
    job = SimpleNamespace(id="test-job-id")
    prepared = await processor.prepare(job, file_docs)
    assert prepared["paths"] == [path_b, path_a, path_c]

    result = await processor.execute(job, file_docs, prepared)
    output_path = result["output_path"]
    try:
        assert os.path.exists(output_path)
        reader = PyPDF2.PdfReader(output_path)
        assert len(reader.pages) == 3
        assert "MARKER-B" in reader.pages[0].extract_text()
        assert "MARKER-A" in reader.pages[1].extract_text()
        assert "MARKER-C" in reader.pages[2].extract_text()

        # verify() must accept this as a valid result.
        await processor.verify(job, file_docs, result)
    finally:
        if os.path.exists(output_path):
            os.remove(output_path)


async def test_execute_corrupt_pdf_among_valid_ones_raises_permanent_error_with_fixed_message(
    tmp_path, corrupt_pdf_bytes
):
    """A corrupted/invalid PDF anywhere in the input list must fail the whole
    merge as a `PermanentProcessingError` with a fixed, safe message - not
    PyPDF2's raw internal error text (issue #39 - Batch 5 of the
    #27/#29/#31 error-leak class, same convention as
    `PdfConvertProcessor`/`PdfToWordProcessor` in this same module)."""
    valid_path = _make_marker_pdf(tmp_path, "valid.pdf", "MARKER-VALID")
    corrupt_path = str(tmp_path / "corrupt.pdf")
    with open(corrupt_path, "wb") as f:
        f.write(corrupt_pdf_bytes)

    file_docs = [
        _fake_file_doc(valid_path, "valid.pdf"),
        _fake_file_doc(corrupt_path, "corrupt.pdf"),
    ]

    processor = MergeProcessor()
    job = SimpleNamespace(id="test-job-id-corrupt")
    prepared = await processor.prepare(job, file_docs)

    with pytest.raises(PermanentProcessingError) as exc_info:
        await processor.execute(job, file_docs, prepared)

    assert str(exc_info.value) == "One or more input PDF files are invalid or unreadable"
    assert "startxref" not in str(exc_info.value)  # no raw PyPDF2 text leaked
    assert exc_info.value.__cause__ is not None  # original error still chained for server logs


async def test_execute_os_error_during_merge_is_wrapped_as_transient(tmp_path, monkeypatch):
    path_a = _make_marker_pdf(tmp_path, "a.pdf", "MARKER-A")
    file_docs = [_fake_file_doc(path_a, "a.pdf")]

    def _boom(self, path, **kwargs):
        raise OSError("simulated transient disk hiccup")

    monkeypatch.setattr(PyPDF2.PdfMerger, "append", _boom)

    processor = MergeProcessor()
    job = SimpleNamespace(id="test-job-id-os-error")
    prepared = await processor.prepare(job, file_docs)

    with pytest.raises(TransientProcessingError):
        await processor.execute(job, file_docs, prepared)


async def test_verify_rejects_empty_or_missing_output():
    processor = MergeProcessor()
    job = SimpleNamespace(id="test-job-id-verify")

    with pytest.raises(PermanentProcessingError, match="Merging produced no output"):
        await processor.verify(job, file_docs=[], result={"output_path": None})

    with pytest.raises(PermanentProcessingError, match="Merging produced no output"):
        await processor.verify(
            job, file_docs=[], result={"output_path": "/definitely/not/a/real/path.pdf"}
        )


async def test_cleanup_is_a_noop():
    """Defined explicitly (not inherited from `Processor`, since
    `MergeProcessor` isn't a subclass) so `_run_multi_file_job` can call it
    unconditionally - must simply not raise."""
    result = await MergeProcessor().cleanup(job=object(), file_docs=[], prepared={})
    assert result is None
