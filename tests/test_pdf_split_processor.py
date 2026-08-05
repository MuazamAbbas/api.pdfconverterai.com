"""Unit tests for `app.services.pdf.processors._parse_ranges` and
`SplitProcessor.validate()`'s syntax/bounds-checking (Handbook Part C.4,
ADR-003), mirroring `tests/test_pdf_convert_and_word_processors.py`'s pure
unit style: no Mongo/Redis for the `_parse_ranges` tests, and a lightweight
fake `file_doc`/`job` (`SimpleNamespace`) for the `SplitProcessor.validate()`
tests, against a real on-disk PDF written to `tmp_path`.

Retry classification (`pdf_split` end to end, via real Mongo/Redis-backed Job/
File documents and `app.worker.pdf_split`) lives in
`tests/test_worker_retry_pdf_split.py`, mirroring
`tests/test_worker_retry_pdf_merge.py`.
"""
from types import SimpleNamespace

import pytest

from app.services.jobs.processor import PermanentProcessingError
from app.services.pdf.processors import SplitProcessor, _parse_ranges

# Matches every other test module's module-level marker (see
# tests/test_worker_retry.py's comment) - even though this module's own
# tests never touch Mongo/Redis directly, mixing an unmarked module into a
# full-suite run alongside the many session-loop-pinned modules was observed
# to destabilize the shared Motor client's event loop for whatever ran
# afterward (cascading "Task ... attached to a different loop" failures in
# unrelated, pre-existing files such as tests/test_worker_retry.py). Setting
# this explicitly, consistent with every sibling module, avoids that.
pytestmark = pytest.mark.asyncio(loop_scope="session")


# --- _parse_ranges (pure, no I/O) --------------------------------------------


async def test_parse_ranges_valid_multi_range():
    assert _parse_ranges("1-2,4", page_count=5) == [(1, 2), (4, 4)]


async def test_parse_ranges_single_page_edge_case():
    assert _parse_ranges("1-1", page_count=1) == [(1, 1)]


@pytest.mark.parametrize(
    "ranges_str",
    ["abc", "1-2-3", "1-", "-5", "1-a", "a-1", ",", "1,,2"],
)
async def test_parse_ranges_rejects_malformed_syntax(ranges_str):
    with pytest.raises(PermanentProcessingError) as exc_info:
        _parse_ranges(ranges_str, page_count=10)
    assert str(exc_info.value) == "Invalid page range syntax"


async def test_parse_ranges_rejects_start_greater_than_end():
    with pytest.raises(PermanentProcessingError) as exc_info:
        _parse_ranges("5-2", page_count=10)
    assert str(exc_info.value) == "A range's start page cannot be after its end page"


@pytest.mark.parametrize("ranges_str", ["0-3", "0"])
async def test_parse_ranges_rejects_page_number_zero_or_below(ranges_str):
    with pytest.raises(PermanentProcessingError) as exc_info:
        _parse_ranges(ranges_str, page_count=10)
    assert str(exc_info.value) == "Page numbers must be greater than 0"


async def test_parse_ranges_rejects_out_of_bounds_page_number():
    with pytest.raises(PermanentProcessingError) as exc_info:
        _parse_ranges("1-11", page_count=10)
    assert str(exc_info.value) == "Page range exceeds the document's page count (10)"


async def test_parse_ranges_rejects_empty_string():
    """`"".split(",")` yields `[""]`, not `[]` - so an empty string is
    rejected by the in-loop empty-part check (same "Invalid page range
    syntax" message as any other empty segment), not the trailing `if not
    parsed` guard. That guard is unreachable through this function alone
    (every code path either raises inside the loop or appends something) -
    it only exists as a defensive backstop; the router-level empty/
    whitespace check (`POST /pdf/split`'s `RANGES_INVALID`) is what actually
    prevents an empty ranges string from reaching here in production."""
    with pytest.raises(PermanentProcessingError) as exc_info:
        _parse_ranges("", page_count=10)
    assert str(exc_info.value) == "Invalid page range syntax"


async def test_parse_ranges_rejects_whitespace_only_string():
    with pytest.raises(PermanentProcessingError) as exc_info:
        _parse_ranges("   ", page_count=10)
    assert str(exc_info.value) == "Invalid page range syntax"


# --- SplitProcessor.validate() ------------------------------------------------


def _fake_pdf_file_doc(tmp_path, filename: str, content: bytes):
    storage_path = str(tmp_path / filename)
    with open(storage_path, "wb") as f:
        f.write(content)
    return SimpleNamespace(originalFilename=filename, storagePath=storage_path)


async def test_validate_rejects_missing_ranges_param(tmp_path, test_pdf_bytes):
    file_doc = _fake_pdf_file_doc(tmp_path, "no-ranges.pdf", test_pdf_bytes)
    job = SimpleNamespace(params=None)

    with pytest.raises(PermanentProcessingError) as exc_info:
        await SplitProcessor().validate(job, file_doc)
    assert str(exc_info.value) == "No page ranges were provided"


async def test_validate_rejects_empty_ranges_param(tmp_path, test_pdf_bytes):
    file_doc = _fake_pdf_file_doc(tmp_path, "empty-ranges.pdf", test_pdf_bytes)
    job = SimpleNamespace(params={"ranges": "   "})

    with pytest.raises(PermanentProcessingError) as exc_info:
        await SplitProcessor().validate(job, file_doc)
    assert str(exc_info.value) == "No page ranges were provided"


async def test_validate_rejects_corrupt_pdf_with_hardcoded_message(tmp_path, corrupt_pdf_bytes):
    file_doc = _fake_pdf_file_doc(tmp_path, "corrupt.pdf", corrupt_pdf_bytes)
    job = SimpleNamespace(params={"ranges": "1-1"})

    with pytest.raises(PermanentProcessingError) as exc_info:
        await SplitProcessor().validate(job, file_doc)
    assert str(exc_info.value) == "Invalid or unreadable PDF file"
    # Never leak PyPDF2's raw parser text (same discipline as issue #39 -
    # Batch 5 of the #27/#29/#31 error-leak class).
    assert "startxref" not in str(exc_info.value)
    assert exc_info.value.__cause__ is not None


async def test_validate_rejects_out_of_bounds_range_against_real_page_count(
    tmp_path, test_pdf_bytes
):
    """`test_pdf_bytes` is a real, valid single-page PDF - requesting page 2
    must be rejected using the PDF's *actual* page count (1), not just
    whatever the caller claims."""
    file_doc = _fake_pdf_file_doc(tmp_path, "single-page.pdf", test_pdf_bytes)
    job = SimpleNamespace(params={"ranges": "1-2"})

    with pytest.raises(PermanentProcessingError) as exc_info:
        await SplitProcessor().validate(job, file_doc)
    assert str(exc_info.value) == "Page range exceeds the document's page count (1)"


async def test_validate_accepts_single_page_edge_case_against_real_page_count(
    tmp_path, test_pdf_bytes
):
    file_doc = _fake_pdf_file_doc(tmp_path, "single-page-ok.pdf", test_pdf_bytes)
    job = SimpleNamespace(params={"ranges": "1-1"})

    await SplitProcessor().validate(job, file_doc)
    assert SplitProcessor is not None  # validate() didn't raise - success
