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
"""
import logging
import os

from app.core.storage import STORAGE_PATH
from app.services.jobs.processor import PermanentProcessingError, Processor, TransientProcessingError
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

    async def execute(self, job, file_doc, prepared):
        try:
            text = await extract_text_from_pdf(prepared["path"])
        except ValueError as e:
            raise PermanentProcessingError(str(e)) from e
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

    async def execute(self, job, file_doc, prepared):
        from app.services.pdf.pdf_to_word import convert_pdf_to_word

        try:
            output_path = await convert_pdf_to_word(prepared["path"], prepared["output_dir"])
        except ValueError as e:
            raise PermanentProcessingError(str(e)) from e
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

    async def execute(self, job, file_doc, prepared):
        from app.services.pdf.summarize import summarize_pdf_service

        try:
            summary = await summarize_pdf_service(prepared["path"])
        except ValueError as e:
            raise PermanentProcessingError(str(e)) from e
        except Exception as e:
            raise TransientProcessingError("Temporary error summarizing the file") from e
        return {"summary": summary}

    async def verify(self, job, file_doc, result):
        if not result.get("summary"):
            raise PermanentProcessingError("Summarization produced no output")
