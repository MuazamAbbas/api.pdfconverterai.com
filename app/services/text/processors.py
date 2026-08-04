"""Concrete Processors for the `text` module's two Tier 2 jobs (Handbook
Part C.4, ADR-003). Each implements the shared Validate/Prepare/Execute/
Verify/Cleanup interface from `app/services/jobs/processor.py`;
`app/worker.py`'s `text_paraphrase`/`text_summarize` task functions are the
thin ARQ-facing wrappers that call `.run()` and own the Job's
Pending/Queued/Processing/Completed/Failed transitions - mirrors
`app/services/pdf/processors.py`.

`text` depends on `jobs` here (imports its Processor base), matching the
Handbook Part C.3 one-direction dependency chain (... file -> job ->
pdf/image/ai) - `jobs` never imports anything from `text`.

`TextParaphraseProcessor`/`TextSummarizeProcessor` don't load their own
models: the t5-small `text2text-generation`/`summarization` pipelines are
loaded once per worker process in `app/worker.py`'s `on_startup` hook and
passed down through `Processor.run(job, file_doc, ctx)` ->
`execute(job, file_doc, prepared, ctx)` as `ctx["paraphrase_pipeline"]`/
`ctx["summarize_pipeline"]` - matching `PdfSummarizeProcessor`'s exact
`ctx["summarizer_pipeline"]` handling.
"""
import logging
import os

from app.services.jobs.processor import (
    PermanentProcessingError,
    Processor,
    TransientProcessingError,
)

logger = logging.getLogger(__name__)


def _read_text_input(file_doc) -> str:
    """Read the small text/URL input file this job processes.

    Unlike `app/services/pdf/processors.py`'s `_validate_pdf_input` (a real
    binary upload), the file backing a `text_*` job is always a small
    UTF-8 text file written by `save_text_input`
    (`app/services/files/service.py`) - read directly as text, not binary.
    """
    if not os.path.exists(file_doc.storagePath):
        raise PermanentProcessingError("Source file is missing or has expired")
    with open(file_doc.storagePath, "r", encoding="utf-8") as f:
        return f.read()


class TextParaphraseProcessor(Processor):
    """job.type == "text_paraphrase" """

    async def validate(self, job, file_doc):
        text = _read_text_input(file_doc)
        if not text or not text.strip():
            raise PermanentProcessingError("Text must be a non-empty string")

    async def prepare(self, job, file_doc):
        return {"text": _read_text_input(file_doc)}

    async def execute(self, job, file_doc, prepared, ctx=None):
        from app.services.text.paraphrase import paraphrase_text

        pipeline = (ctx or {}).get("paraphrase_pipeline")
        if pipeline is None:
            # Worker process hasn't finished on_startup yet, or ctx wasn't
            # threaded through - a temporary condition, safe to retry.
            raise TransientProcessingError("Paraphrase model is not loaded yet")
        try:
            paraphrased = await paraphrase_text(prepared["text"], pipeline)
        except ValueError as e:
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from `paraphrase_text`'s text
            # (issue #39 - Batch 5 of the #27/#29/#31 error leak class).
            raise PermanentProcessingError("Text must be a non-empty string") from e
        except Exception as e:
            raise TransientProcessingError("Temporary error paraphrasing the text") from e
        return {"paraphrased": paraphrased}

    async def verify(self, job, file_doc, result):
        if not result.get("paraphrased"):
            raise PermanentProcessingError("Paraphrasing produced no output")


class TextSummarizeProcessor(Processor):
    """job.type == "text_summarize" """

    async def validate(self, job, file_doc):
        text = _read_text_input(file_doc)
        if len(text) < 50:
            raise PermanentProcessingError("Text must be at least 50 characters")

    async def prepare(self, job, file_doc):
        return {"text": _read_text_input(file_doc)}

    async def execute(self, job, file_doc, prepared, ctx=None):
        from app.services.text.summarize import summarize_text_service

        pipeline = (ctx or {}).get("summarize_pipeline")
        if pipeline is None:
            # Worker process hasn't finished on_startup yet, or ctx wasn't
            # threaded through - a temporary condition, safe to retry.
            raise TransientProcessingError("Summarization model is not loaded yet")
        try:
            summary = await summarize_text_service(prepared["text"], pipeline)
        except ValueError as e:
            # Fixed, hardcoded message rather than `str(e)` - decouples this
            # client-facing contract from `summarize_text_service`'s text
            # (issue #39 - Batch 5 of the #27/#29/#31 error leak class).
            raise PermanentProcessingError("Text must be at least 50 characters") from e
        except Exception as e:
            raise TransientProcessingError("Temporary error summarizing the text") from e
        return {"summary": summary}

    async def verify(self, job, file_doc, result):
        if not result.get("summary"):
            raise PermanentProcessingError("Summarization produced no output")
