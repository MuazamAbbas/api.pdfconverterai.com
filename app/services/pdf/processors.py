"""Concrete Processors for the three Tier 2 PDF jobs (Handbook Part C.4,
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
