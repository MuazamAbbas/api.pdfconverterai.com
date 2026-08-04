"""ARQ worker process entrypoint for Tier 2 PDF/Image/Text/Web Tools/
Downloaders jobs (Handbook Part C.2/C.4/C.7, ADR-003 Processing Engine,
ADR-006 ARQ). Run with:

    arq app.worker.WorkerSettings

Each task function below is a thin wrapper: fetch the Job + input File,
transition Pending/Queued -> Processing, run the matching Processor's
Validate/Prepare/Execute/Verify/Cleanup pipeline
(`app/services/jobs/processor.py`), then transition to Completed/Failed.

Heavy per-tool imports (pdf2docx for `pdf_to_word`) are deferred into each
task function rather than imported at module scope, so the worker process
itself starts up quickly regardless of which job types happen to run.

The `facebook/bart-large-cnn` model used by `pdf_summarize`, and the
t5-small `text2text-generation`/`summarization` models used by
`text_paraphrase`/`text_summarize`/`web_tools_summarize`, are the exception:
each is loaded once per worker process in `on_startup()` below (ARQ's
worker-process analogue of `app.state` in `app/main.py`) and handed to the
matching job via `ctx["summarizer_pipeline"]`/`ctx["paraphrase_pipeline"]`/
`ctx["summarize_pipeline"]`, rather than being loaded eagerly at import time
or read off `app.state` in the FastAPI request process (ADR-015 Open Item
2 - the t5-small pipelines moved here from `app/main.py`'s `startup_event()`
once `text`/`web_tools` summarize/paraphrase became Tier 2 jobs). The
`transformers` import itself still only happens inside `on_startup()`, not
at this module's top level, so importing `app.worker` stays light for any
process that just needs `WorkerSettings`.
"""
import logging
import os

from app.core.logging import setup_logging

# Must run before any other import below. `arq`'s CLI (arq/cli.py) imports
# this module *then* calls `logging.config.dictConfig(...)` itself - that
# dictConfig only ever adds a handler to the 'arq' logger namespace, so
# without this call the worker process's own `app.*` loggers (job
# completed/retrying/failed - see `_run_job` below) would have no handler
# on the root logger at all, same fragility this fixes in app/main.py.
setup_logging()

from arq import Retry, func  # noqa: E402
from arq.connections import RedisSettings  # noqa: E402

from app.core.config import settings  # noqa: E402
from app.services.files.service import get_file_by_id, save_output_file  # noqa: E402
from app.services.jobs.processor import (  # noqa: E402
    PermanentProcessingError,
    TransientProcessingError,
)
from app.services.jobs.service import (  # noqa: E402
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
        # `logger.exception` (not `.warning`) so the real underlying error -
        # e.g. the raw `yt_dlp` exception chained via `raise ... from e` in
        # `downloaders/processors.py` - still lands server-side even though
        # `str(e)` here is now a deliberately generic, client-safe message.
        logger.exception("Job %s (%s) failed permanently: %s", job_id, job.type, str(e))
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


async def text_paraphrase(ctx, job_id: str) -> None:
    from app.services.text.processors import TextParaphraseProcessor

    async def build_result(job, file_doc, raw_result):
        return raw_result  # {"paraphrased": "..."}

    await _run_job(ctx, job_id, TextParaphraseProcessor, build_result)


async def text_summarize(ctx, job_id: str) -> None:
    from app.services.text.processors import TextSummarizeProcessor

    async def build_result(job, file_doc, raw_result):
        return raw_result  # {"summary": "..."}

    await _run_job(ctx, job_id, TextSummarizeProcessor, build_result)


async def web_tools_summarize(ctx, job_id: str) -> None:
    from app.services.web_tools.processors import WebToolsSummarizeProcessor

    async def build_result(job, file_doc, raw_result):
        return raw_result  # {"summary": "..."}

    await _run_job(ctx, job_id, WebToolsSummarizeProcessor, build_result)


async def downloaders_youtube(ctx, job_id: str) -> None:
    from app.services.downloaders.processors import DownloadersYoutubeProcessor

    async def build_result(job, file_doc, raw_result):
        base_name = os.path.splitext(file_doc.originalFilename)[0]
        title = raw_result.get("title") or base_name
        ext = raw_result.get("ext") or "mp4"
        output_doc = await save_output_file(
            local_path=raw_result["output_path"],
            owner_api_key_id=file_doc.ownerApiKeyId,
            original_filename=f"{title}.{ext}",
            # `format: "best"` doesn't guarantee mp4 - see
            # `app/services/downloaders/processors.py`'s `_EXT_MIME_TYPES`
            # comment for the known limitation (falls back to
            # application/octet-stream for an unmapped container).
            mime_type=raw_result.get("mime_type", "application/octet-stream"),
        )
        return {"outputFileId": str(output_doc.id)}

    await _run_job(ctx, job_id, DownloadersYoutubeProcessor, build_result)


async def on_startup(ctx: dict) -> None:
    """Runs once per ARQ worker process (Handbook Part C.2/C.7, ADR-006) -
    the worker-process analogue of `app.state` in `app/main.py`. Preloads
    the `facebook/bart-large-cnn` summarization pipeline plus the t5-small
    paraphrase/summarization pipelines (formerly loaded into `app.state` at
    FastAPI startup - ADR-015 Open Item 2 - now that `text_paraphrase`/
    `text_summarize`/`web_tools_summarize` are Tier 2 jobs run from this
    worker process instead) into `ctx` so every job of that type reuses them
    instead of reloading per-job. `transformers` is imported here, not at
    module scope, keeping this module's own import light for anything that
    doesn't need it (see module docstring).
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

    logger.info("Worker starting up: preloading t5-small paraphrase/summarize pipelines")
    try:
        paraphrase_pipeline = pipeline("text2text-generation", model="t5-small", device="cpu")
    except Exception as e:
        logger.error("Failed to load paraphrase_pipeline: %s", str(e))
        raise
    if not hasattr(paraphrase_pipeline, "model"):
        logger.error("paraphrase_pipeline is invalid or not initialized")
        raise ValueError("Invalid paraphrase_pipeline")
    ctx["paraphrase_pipeline"] = paraphrase_pipeline

    try:
        summarize_pipeline = pipeline("summarization", model="t5-small", device="cpu")
    except Exception as e:
        logger.error("Failed to load summarize_pipeline: %s", str(e))
        raise
    if not hasattr(summarize_pipeline, "model"):
        logger.error("summarize_pipeline is invalid or not initialized")
        raise ValueError("Invalid summarize_pipeline")
    ctx["summarize_pipeline"] = summarize_pipeline
    logger.info("Worker startup complete: paraphrase_pipeline/summarize_pipeline loaded")


async def on_shutdown(ctx: dict) -> None:
    logger.info("Worker shutting down: cleaning up summarizer_pipeline/paraphrase_pipeline/summarize_pipeline")
    ctx.pop("summarizer_pipeline", None)
    ctx.pop("paraphrase_pipeline", None)
    ctx.pop("summarize_pipeline", None)


class WorkerSettings:
    functions = [
        pdf_convert,
        pdf_to_word,
        pdf_summarize,
        image_ocr,
        text_paraphrase,
        text_summarize,
        web_tools_summarize,
        # `downloaders_youtube` gets its own longer `timeout` (arq 0.28.0's
        # `func()` supports a genuine per-function override, confirmed
        # against the installed arq version rather than assumed - see
        # `arq.worker.Function`/`arq.worker.func`). A full video download
        # has fundamentally different latency characteristics than the
        # other jobs below sharing the flat `job_timeout = 300` (5 min)
        # default - 600s (10 min) gives realistically long/slow-connection
        # downloads room to finish without regressing the 5-minute budget
        # every other job type here still gets.
        func(downloaders_youtube, timeout=600),
    ]
    on_startup = on_startup
    on_shutdown = on_shutdown
    redis_settings = RedisSettings.from_dsn(settings.redis_url)
    max_tries = MAX_TRIES
    # Global fallback for every job type above that isn't given its own
    # `func(..., timeout=...)` override (Handbook Part C.4 - see
    # `downloaders_youtube`'s per-function override above for the one
    # exception, and its comment for why it needs one).
    job_timeout = 300
