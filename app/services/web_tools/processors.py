"""Concrete Processor for the `web_tools` module's single Tier 2 job,
`web_tools_summarize` (Handbook Part C.4, ADR-003). Mirrors
`app/services/pdf/processors.py`/`app/services/text/processors.py` -
implements the shared Validate/Prepare/Execute/Verify/Cleanup interface
from `app/services/jobs/processor.py`; `app/worker.py`'s
`web_tools_summarize` task function is the thin ARQ-facing wrapper that
calls `.run()` and owns the Job's Pending/Queued/Processing/Completed/
Failed transitions.

`web_tools` depends on `jobs` here (imports its Processor base), matching
the Handbook Part C.3 one-direction dependency chain (... file -> job ->
pdf/image/ai/web_tools) - `jobs` never imports anything from `web_tools`.

Reuses the same shared t5-small summarization pipeline
`app/services/text/processors.py`'s `TextSummarizeProcessor` uses -
`ctx["summarize_pipeline"]`, loaded once per worker process in
`app/worker.py`'s `on_startup` hook - intentionally, matching the prior
code's existing sharing of one `Summarizer`/pipeline across both `text`
and `web_tools` summarization.
"""
import logging
import os

import aiohttp

from app.services.jobs.processor import (
    PermanentProcessingError,
    Processor,
    TransientProcessingError,
)

logger = logging.getLogger(__name__)


def _read_url_input(file_doc) -> str:
    """Read the small text file holding the URL this job processes (written
    by `save_text_input`, `app/services/files/service.py`).
    """
    if not os.path.exists(file_doc.storagePath):
        raise PermanentProcessingError("Source file is missing or has expired")
    with open(file_doc.storagePath, "r", encoding="utf-8") as f:
        return f.read().strip()


class WebToolsSummarizeProcessor(Processor):
    """job.type == "web_tools_summarize" """

    async def validate(self, job, file_doc):
        url = _read_url_input(file_doc)
        if not url.startswith(("http://", "https://")):
            raise PermanentProcessingError("URL must start with http:// or https://")

    async def prepare(self, job, file_doc):
        return {"url": _read_url_input(file_doc)}

    async def execute(self, job, file_doc, prepared, ctx=None):
        from app.services.web_tools.summarize import fetch_webpage_text, summarize_webpage_text

        try:
            text = await fetch_webpage_text(prepared["url"])
        except aiohttp.ClientError as e:
            # Network hiccup fetching the page - safe to retry.
            raise TransientProcessingError("Temporary error fetching the webpage") from e
        except ValueError as e:
            # No text extracted / text too short - a permanent input problem.
            # Deliberately a fixed, hardcoded message rather than `str(e)`:
            # `fetch_webpage_text` only ever raises this ValueError with an
            # already-safe literal string today, but hardcoding here
            # decouples this client-facing contract from that service
            # function's text (issue #39 - Batch 5 of the #27/#29/#31 error
            # leak class; see `downloaders/processors.py` for the
            # originally-motivating case, a genuinely raw library exception).
            raise PermanentProcessingError(
                "No text extracted from the webpage, or it is too short"
            ) from e

        pipeline = (ctx or {}).get("summarize_pipeline")
        if pipeline is None:
            # Worker process hasn't finished on_startup yet, or ctx wasn't
            # threaded through - a temporary condition, safe to retry.
            raise TransientProcessingError("Summarization model is not loaded yet")
        try:
            summary = await summarize_webpage_text(text, pipeline)
        except Exception as e:
            raise TransientProcessingError("Temporary error summarizing the webpage") from e
        return {"summary": summary}

    async def verify(self, job, file_doc, result):
        if not result.get("summary"):
            raise PermanentProcessingError("Summarization produced no output")
