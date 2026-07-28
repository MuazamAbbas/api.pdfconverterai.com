"""ARQ worker process entrypoint for Tier 2 PDF/Image jobs (Handbook Part
C.2/C.4/C.7, ADR-003 Processing Engine, ADR-006 ARQ). Run with:

    arq app.worker.WorkerSettings

Each task function below is a thin wrapper: fetch the Job + input File,
transition Pending/Queued -> Processing, run the matching Processor's
Validate/Prepare/Execute/Verify/Cleanup pipeline
(`app/services/jobs/processor.py`), then transition to Completed/Failed.

Heavy per-tool imports (pdf2docx for `pdf_to_word`) are deferred into each
task function rather than imported at module scope, so the worker process
itself starts up quickly regardless of which job types happen to run.

The `facebook/bart-large-cnn` model used by `pdf_summarize` is the one
exception: it's loaded once per worker process in `on_startup()` below
(ARQ's worker-process analogue of `app.state` in `app/main.py`) and handed
to each `pdf_summarize` job via `ctx["summarizer_pipeline"]`, rather than
being loaded eagerly at import time inside `app/services/pdf/summarize.py`
(ADR-015 Open Item 2). The `transformers` import itself still only happens
inside `on_startup()`, not at this module's top level, so importing
`app.worker` stays light for any process that just needs `WorkerSettings`.
"""
import logging
import os

from arq import Retry
from arq.connections import RedisSettings

from app.core.config import settings
from app.services.files.service import get_file_by_id, save_output_file
from app.services.jobs.processor import PermanentProcessingError, TransientProcessingError
from app.services.jobs.service import (
    get_job,
    increment_retry_count,
    mark_completed,
    mark_failed,
    mark_processing,
)

logger = logging.getLogger(__name__)

# Matches JobDocument.maxRetries' default (Handbook Part C.4 / ADR-003).
MAX_TRIES = 3


async def _run_job(ctx, job_id: str, make_processor, build_result) -> None:
    """Shared orchestration for every PDF job type.

    `make_processor` builds the Processor instance (called inside here so
    each task function's heavy import stays lazy). `build_result` turns the
    Processor's raw result into what gets stored on `jobs.result` (e.g.
    registering an output file for `pdf_to_word`).
    """
    job = await get_job(job_id)
    if job is None:
        logger.error("Job %s not found - dropping (likely expired)", job_id)
        return

    file_doc = await get_file_by_id(str(job.fileId))
    if file_doc is None:
        logger.warning("Job %s references a missing/expired file %s", job_id, job.fileId)
        await mark_failed(job_id, "Input file not found or has expired")
        return

    await mark_processing(job_id)
    processor = make_processor()
    try:
        raw_result = await processor.run(job, file_doc, ctx)
        result = await build_result(job, file_doc, raw_result)
        await mark_completed(job_id, result)
        logger.info("Job %s (%s) completed", job_id, job.type)
    except PermanentProcessingError as e:
        logger.warning("Job %s (%s) failed permanently: %s", job_id, job.type, str(e))
        await mark_failed(job_id, str(e))
    except TransientProcessingError as e:
        job_try = ctx.get("job_try", 1)
        if job_try >= MAX_TRIES:
            logger.error("Job %s (%s) exhausted retries: %s", job_id, job.type, str(e))
            await mark_failed(job_id, "Processing failed after multiple attempts")
        else:
            await increment_retry_count(job_id)
            logger.info(
                "Job %s (%s) transient failure, retrying (attempt %s/%s): %s",
                job_id, job.type, job_try, MAX_TRIES, str(e),
            )
            raise Retry(defer=min(2**job_try, 30))
    except Exception as e:
        logger.exception("Job %s (%s) hit an unexpected error: %s", job_id, job.type, str(e))
        await mark_failed(job_id, "An unexpected error occurred while processing the file")


async def pdf_convert(ctx, job_id: str) -> None:
    from app.services.pdf.processors import PdfConvertProcessor

    async def build_result(job, file_doc, raw_result):
        return raw_result  # {"text": "..."}

    await _run_job(ctx, job_id, PdfConvertProcessor, build_result)


async def pdf_to_word(ctx, job_id: str) -> None:
    from app.services.pdf.processors import PdfToWordProcessor

    async def build_result(job, file_doc, raw_result):
        base_name = os.path.splitext(file_doc.originalFilename)[0] + ".docx"
        output_doc = await save_output_file(
            local_path=raw_result["output_path"],
            owner_api_key_id=file_doc.ownerApiKeyId,
            original_filename=base_name,
            mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        return {"outputFileId": str(output_doc.id)}

    await _run_job(ctx, job_id, PdfToWordProcessor, build_result)


async def pdf_summarize(ctx, job_id: str) -> None:
    from app.services.pdf.processors import PdfSummarizeProcessor

    async def build_result(job, file_doc, raw_result):
        return raw_result  # {"summary": "..."}

    await _run_job(ctx, job_id, PdfSummarizeProcessor, build_result)


async def image_ocr(ctx, job_id: str) -> None:
    from app.services.image.processors import ImageOcrProcessor

    async def build_result(job, file_doc, raw_result):
        return raw_result  # {"text": "..."}

    await _run_job(ctx, job_id, ImageOcrProcessor, build_result)


async def on_startup(ctx: dict) -> None:
    """Runs once per ARQ worker process (Handbook Part C.2/C.7, ADR-006) -
    the worker-process analogue of `app.state` in `app/main.py`. Preloads
    the `facebook/bart-large-cnn` summarization pipeline into `ctx` so every
    `pdf_summarize` job reuses it instead of reloading per-job. `transformers`
    is imported here, not at module scope, keeping this module's own import
    light for anything that doesn't need it (see module docstring).
    """
    from transformers import pipeline

    logger.info("Worker starting up: preloading facebook/bart-large-cnn")
    try:
        summarizer_pipeline = pipeline("summarization", model="facebook/bart-large-cnn", device="cpu")
    except Exception as e:
        logger.error("Failed to load summarizer_pipeline: %s", str(e))
        raise
    if not hasattr(summarizer_pipeline, "model"):
        logger.error("summarizer_pipeline is invalid or not initialized")
        raise ValueError("Invalid summarizer_pipeline")
    ctx["summarizer_pipeline"] = summarizer_pipeline
    logger.info("Worker startup complete: summarizer_pipeline loaded")


async def on_shutdown(ctx: dict) -> None:
    logger.info("Worker shutting down: cleaning up summarizer_pipeline")
    ctx.pop("summarizer_pipeline", None)


class WorkerSettings:
    functions = [pdf_convert, pdf_to_word, pdf_summarize, image_ocr]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_tries = MAX_TRIES
    job_timeout = 300
