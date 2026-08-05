"""Concrete Processors for the Tier 2 PDF jobs (Handbook Part C.4,
ADR-003). Each implements the shared Validate/Prepare/Execute/Verify/
Cleanup interface from `app/services/jobs/processor.py`; `app/worker.py`'s
task functions are the thin ARQ-facing wrappers that call `.run()` and own
the Job's Pending/Queued/Processing/Completed/Failed transitions.

`pdf` depends on `jobs` here (imports its Processor base), matching the
Handbook Part C.3 one-direction dependency chain (... file -> job -> pdf) -
`jobs` never imports anything from `pdf`.

`convert_pdf_to_word`/`summarize_pdf_service` are imported inside their
respective `execute()` methods rather than at module scope, so importing
this module (or running a `pdf_convert` job) never pulls in pdf2docx or
transformers/torch unless a `pdf_to_word`/`pdf_summarize` job actually
runs - see `app/worker.py`'s module docstring for why that matters.

`PdfSummarizeProcessor` no longer loads its own model: the `bart-large-cnn`
pipeline is loaded once per worker process in `app/worker.py`'s
`on_startup` hook and passed down through `Processor.run(job, file_doc,
ctx)` -> `execute(job, file_doc, prepared, ctx)` as `ctx["summarizer_pipeline"]`.

`SplitProcessor` (job.type == "pdf_split") reads its `ranges` input off
`job.params["ranges"]` (see `app/schemas/job.py`'s `JobBase.params`) rather
than a new method parameter, since `job` already flows through every step
of `Processor.run()` unchanged - keeps the base `Processor` interface
untouched for every other processor in this file. It parses and validates
that string in `validate()` (needs the PDF's real page count, opened via
PyPDF2 there) and stashes the parsed `(start, end)` tuples on `self` for
`prepare()` to pick up - safe because `app/worker.py` creates one fresh
Processor instance per job run (see `_run_job`'s `make_processor()` call).

`MergeProcessor` (job.type == "pdf_merge") is the first multi-file Tier 2
tool (Handbook Part I.2) and deliberately does NOT subclass `Processor`
(`app/services/jobs/processor.py`) - the base class's
`validate/prepare/execute/verify/cleanup`/`run()` signatures are all
single-`file_doc` (Handbook Part C.4's existing Processor interface), and
changing that base class would affect every other processor in this file.
Instead `MergeProcessor` mirrors the same Validate -> Prepare -> Execute ->
Verify shape with every method taking `file_docs: list` (in the order the
caller submitted `fileIds`) instead of `file_doc`. `app/worker.py`'s
`_run_multi_file_job` calls these four methods directly, in the same
sequence `Processor.run()` uses for every other job type, since it can't
route through the unmodified `.run()` here.
"""
import logging
import os
import zipfile

import PyPDF2

from app.core.storage import STORAGE_PATH
from app.services.jobs.processor import (
    PermanentProcessingError,
    Processor,
    TransientProcessingError,
)
from app.services.pdf.convert import extract_text_from_pdf

logger = logging.getLogger(__name__)


def _validate_pdf_input(file_doc) -> None:
    if not file_doc.originalFilename.lower().endswith(".pdf"):
        raise PermanentProcessingError("File must be a PDF")
    if not os.path.exists(file_doc.storagePath):
        raise PermanentProcessingError("Source file is missing or has expired")


# `SplitProcessor.execute()` does one `PyPDF2.PdfWriter` instantiation +
# disk write per accepted range, plus one `zipfile` entry per range - all
# driven by this attacker-controlled free-text field. Without a cap, a
# tiny (few-KB) many-thousand-blank-page PDF plus a long `ranges` string
# (e.g. the same in-bounds page repeated thousands of times, or "1,2,...,N")
# turns one job into thousands of file writes/zip operations - real
# resource amplification versus the ~25MB single-input-file cap every other
# single-file PDF job is implicitly bounded by (`max_upload_size_mb`,
# `app/core/config.py`). Mirrors the aggregate-cap pattern
# `MAX_MERGE_TOTAL_BYTES` uses in `app/routers/pdf.py` for the same DoS
# class on `/pdf/merge`, but lives here (not the router) since this is
# where the range string is actually parsed and the real page count is
# available.
#
# 100 output files from a single split request comfortably covers every
# legitimate use case (splitting a document into per-chapter/per-page
# files) while keeping worst-case PdfWriter/zip work per job small. 1000
# total output pages keeps the sum of per-range page spans (a single
# `"1-100000"` range would otherwise bypass the range-count cap entirely)
# proportional to a realistically-sized document rather than attacker-
# inflated by a cheap, mostly-blank-page PDF.
MAX_SPLIT_RANGES = 100
MAX_SPLIT_OUTPUT_PAGES = 1000


def _parse_ranges(ranges_str: str, page_count: int) -> list[tuple[int, int]]:
    """Parse a `"1-3,5,7-9"`-style page-range string into a list of
    1-indexed, inclusive `(start, end)` tuples, validated against the PDF's
    real `page_count`.

    Also enforces `MAX_SPLIT_RANGES` (number of comma-separated segments)
    and `MAX_SPLIT_OUTPUT_PAGES` (sum of pages across all accepted ranges)
    - see the comment above those constants for why.

    Every rejection path raises `PermanentProcessingError` with a fixed,
    client-safe message rather than surfacing raw parser internals (same
    error-leak-class discipline as issues #27/#29/#31/#39) - there's no
    third-party exception here to leak from, but the messages stay generic
    on principle/consistency with the rest of this module.
    """
    parsed: list[tuple[int, int]] = []
    total_output_pages = 0
    for raw_part in ranges_str.split(","):
        part = raw_part.strip()
        if not part:
            raise PermanentProcessingError("Invalid page range syntax")

        if len(parsed) >= MAX_SPLIT_RANGES:
            raise PermanentProcessingError(
                f"Too many page ranges (maximum {MAX_SPLIT_RANGES})"
            )

        if "-" in part:
            segments = part.split("-")
            if len(segments) != 2:
                raise PermanentProcessingError("Invalid page range syntax")
            start_str, end_str = segments[0].strip(), segments[1].strip()
            if not start_str.isdigit() or not end_str.isdigit():
                raise PermanentProcessingError("Invalid page range syntax")
            start, end = int(start_str), int(end_str)
        else:
            if not part.isdigit():
                raise PermanentProcessingError("Invalid page range syntax")
            start = end = int(part)

        if start <= 0 or end <= 0:
            raise PermanentProcessingError("Page numbers must be greater than 0")
        if start > end:
            raise PermanentProcessingError("A range's start page cannot be after its end page")
        if end > page_count:
            raise PermanentProcessingError(
                f"Page range exceeds the document's page count ({page_count})"
            )

        total_output_pages += end - start + 1
        if total_output_pages > MAX_SPLIT_OUTPUT_PAGES:
            raise PermanentProcessingError(
                f"Total requested output pages exceed the maximum of {MAX_SPLIT_OUTPUT_PAGES}"
            )

        parsed.append((start, end))

    if not parsed:
        raise PermanentProcessingError("No valid page ranges were provided")
    return parsed


class PdfConvertProcessor(Processor):
    """job.type == "pdf_convert" """

    async def validate(self, job, file_doc):
        _validate_pdf_input(file_doc)

    async def prepare(self, job, file_doc):
        return {"path": file_doc.storagePath}

    async def execute(self, job, file_doc, prepared, ctx=None):
        try:
            text = await extract_text_from_pdf(prepared["path"])
        except ValueError as e:
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from `extract_text_from_pdf`'s text
            # (issue #39 - Batch 5 of the #27/#29/#31 error leak class).
            raise PermanentProcessingError(
                "Invalid or unreadable PDF file, or no text could be extracted"
            ) from e
        except OSError as e:
            raise TransientProcessingError("Temporary I/O error while reading the file") from e
        return {"text": text}

    async def verify(self, job, file_doc, result):
        if not result.get("text"):
            raise PermanentProcessingError("No text could be extracted from this PDF")


class PdfToWordProcessor(Processor):
    """job.type == "pdf_to_word" """

    async def validate(self, job, file_doc):
        _validate_pdf_input(file_doc)

    async def prepare(self, job, file_doc):
        os.makedirs(STORAGE_PATH, exist_ok=True)
        return {"path": file_doc.storagePath, "output_dir": STORAGE_PATH}

    async def execute(self, job, file_doc, prepared, ctx=None):
        from app.services.pdf.pdf_to_word import convert_pdf_to_word

        try:
            output_path = await convert_pdf_to_word(prepared["path"], prepared["output_dir"])
        except ValueError as e:
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from `convert_pdf_to_word`'s text
            # (issue #39 - Batch 5 of the #27/#29/#31 error leak class).
            raise PermanentProcessingError("Source PDF file is missing or has expired") from e
        except Exception as e:
            raise TransientProcessingError("Temporary error converting the file") from e
        return {"output_path": output_path}

    async def verify(self, job, file_doc, result):
        output_path = result.get("output_path")
        if not output_path or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise PermanentProcessingError("Word conversion produced no output")


class SplitProcessor(Processor):
    """job.type == "pdf_split"

    Reads its `ranges` input off `job.params["ranges"]` - see this module's
    docstring for why (generic `Job.params` field rather than a new
    `Processor` method parameter).
    """

    async def validate(self, job, file_doc):
        _validate_pdf_input(file_doc)

        ranges_str = (job.params or {}).get("ranges") if job.params else None
        if not ranges_str or not ranges_str.strip():
            raise PermanentProcessingError("No page ranges were provided")

        try:
            reader = PyPDF2.PdfReader(file_doc.storagePath)
            page_count = len(reader.pages)
        except PyPDF2.errors.PyPdfError as e:
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from PyPDF2's text (issue #39 - Batch 5
            # of the #27/#29/#31 error leak class).
            raise PermanentProcessingError("Invalid or unreadable PDF file") from e
        except OSError as e:
            raise TransientProcessingError("Temporary I/O error while reading the file") from e

        # Stashed on `self` for `prepare()` - see module docstring for why
        # this is safe (one Processor instance per job run).
        self._parsed_ranges = _parse_ranges(ranges_str, page_count)

    async def prepare(self, job, file_doc):
        os.makedirs(STORAGE_PATH, exist_ok=True)
        return {
            "path": file_doc.storagePath,
            "ranges": self._parsed_ranges,
            "output_dir": STORAGE_PATH,
        }

    async def execute(self, job, file_doc, prepared, ctx=None):
        ranges = prepared["ranges"]
        output_dir = prepared["output_dir"]
        zip_path = os.path.join(output_dir, f"split-{job.id}-{os.getpid()}.zip")
        part_paths: list[str] = []
        try:
            try:
                reader = PyPDF2.PdfReader(prepared["path"])
                for index, (start, end) in enumerate(ranges, start=1):
                    writer = PyPDF2.PdfWriter()
                    for page_num in range(start - 1, end):
                        writer.add_page(reader.pages[page_num])
                    part_path = os.path.join(output_dir, f"split-{job.id}-{index}.pdf")
                    with open(part_path, "wb") as f:
                        writer.write(f)
                    part_paths.append(part_path)

                with zipfile.ZipFile(zip_path, "w") as zf:
                    for index, part_path in enumerate(part_paths, start=1):
                        zf.write(part_path, arcname=f"split-{index}.pdf")
            except PyPDF2.errors.PyPdfError as e:
                # Fixed, hardcoded message rather than `str(e)` - decouples
                # this client-facing contract from PyPDF2's text (issue #39
                # - Batch 5 of the #27/#29/#31 error leak class).
                raise PermanentProcessingError(
                    "The PDF file is invalid or could not be split"
                ) from e
            except OSError as e:
                raise TransientProcessingError(
                    "Temporary I/O error while splitting the file"
                ) from e
        finally:
            # Keep only the zip - delete the intermediate per-range PDFs
            # once they're bundled in (or on error, so a failed job doesn't
            # leave orphaned per-range files behind).
            for part_path in part_paths:
                if os.path.exists(part_path):
                    try:
                        os.remove(part_path)
                    except OSError:
                        logger.warning("Failed to remove intermediate split file %s", part_path)
        return {"output_path": zip_path}

    async def verify(self, job, file_doc, result):
        output_path = result.get("output_path")
        if not output_path or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise PermanentProcessingError("Splitting produced no output")


class PdfSummarizeProcessor(Processor):
    """job.type == "pdf_summarize" """

    async def validate(self, job, file_doc):
        _validate_pdf_input(file_doc)

    async def prepare(self, job, file_doc):
        return {"path": file_doc.storagePath}

    async def execute(self, job, file_doc, prepared, ctx=None):
        from app.services.pdf.summarize import summarize_pdf_service

        summarizer = (ctx or {}).get("summarizer_pipeline")
        if summarizer is None:
            # Worker process hasn't finished on_startup yet, or ctx wasn't
            # threaded through - a temporary condition, safe to retry.
            raise TransientProcessingError("Summarization model is not loaded yet")
        try:
            summary = await summarize_pdf_service(prepared["path"], summarizer)
        except ValueError as e:
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from `summarize_pdf_service`'s text
            # (issue #39 - Batch 5 of the #27/#29/#31 error leak class).
            raise PermanentProcessingError(
                "Invalid or unreadable PDF file, or text too short to summarize"
            ) from e
        except Exception as e:
            raise TransientProcessingError("Temporary error summarizing the file") from e
        return {"summary": summary}

    async def verify(self, job, file_doc, result):
        if not result.get("summary"):
            raise PermanentProcessingError("Summarization produced no output")


class MergeProcessor:
    """job.type == "pdf_merge"

    Deliberately NOT a `Processor` subclass - see this module's docstring
    for why. Every method below takes `file_docs: list` (in submitted
    `fileIds` order) instead of a single `file_doc`; `app/worker.py`'s
    `_run_multi_file_job` calls `validate`/`prepare`/`execute`/`verify`
    directly in that order rather than through `Processor.run()`.
    """

    async def validate(self, job, file_docs) -> None:
        for file_doc in file_docs:
            _validate_pdf_input(file_doc)

    async def prepare(self, job, file_docs) -> dict:
        os.makedirs(STORAGE_PATH, exist_ok=True)
        return {
            "paths": [file_doc.storagePath for file_doc in file_docs],
            "output_dir": STORAGE_PATH,
        }

    async def execute(self, job, file_docs, prepared, ctx=None):
        output_path = os.path.join(STORAGE_PATH, f"merged-{job.id}-{os.getpid()}.pdf")
        merger = PyPDF2.PdfMerger()
        try:
            try:
                for path in prepared["paths"]:
                    merger.append(path)
                with open(output_path, "wb") as f:
                    merger.write(f)
            except PyPDF2.errors.PyPdfError as e:
                # Fixed, hardcoded message rather than `str(e)` - decouples
                # this client-facing contract from PyPDF2's text (issue #39
                # - Batch 5 of the #27/#29/#31 error leak class).
                raise PermanentProcessingError(
                    "One or more input PDF files are invalid or unreadable"
                ) from e
            except OSError as e:
                raise TransientProcessingError(
                    "Temporary I/O error while merging the files"
                ) from e
        finally:
            merger.close()
        return {"output_path": output_path}

    async def verify(self, job, file_docs, result) -> None:
        output_path = result.get("output_path")
        if not output_path or not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            raise PermanentProcessingError("Merging produced no output")

    async def cleanup(self, job, file_docs, prepared) -> None:
        """No-op, same as every other processor in this file - the merged
        output at `prepared`/`result`'s `output_path` is kept on disk and
        registered as its own `files` document by `build_result` in
        `app/worker.py`, not cleaned up here. Defined explicitly (rather
        than relied on via inheritance, since `MergeProcessor` doesn't
        subclass `Processor`) so `_run_multi_file_job` can call it
        unconditionally, matching `Processor.run()`'s always-runs-cleanup
        structure.
        """
        return None
