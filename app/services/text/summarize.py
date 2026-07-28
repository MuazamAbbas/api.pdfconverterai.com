import logging

logger = logging.getLogger(__name__)


async def summarize_text_service(text: str, pipeline) -> str:
    """Summarize input text using a preloaded t5-small pipeline.

    Args:
        text (str): Input text to summarize.
        pipeline: Preloaded Hugging Face `summarization` pipeline
            (t5-small), loaded once per worker process in `app/worker.py`'s
            `on_startup` hook and passed in via `ctx["summarize_pipeline"]`
            (Handbook Part C.2/C.4, ADR-003/ADR-006) - this module no
            longer loads its own model or reads it off `app.state`
            (previously reached via a `Summarizer(app)` instance in the
            FastAPI request process; that path is now Tier 2, so the
            pipeline lives in the ARQ worker process's `ctx` instead - see
            `app/services/text/processors.py`'s `TextSummarizeProcessor`).

    Returns:
        str: Summarized text.

    Raises:
        ValueError: If text is empty or too short - treated as a permanent,
            non-retryable failure by the caller (ADR-003).
    """
    if not text.strip():
        logger.error("Text is empty")
        raise ValueError("Text cannot be empty")
    if len(text) < 50:
        logger.error("Text too short: %d characters", len(text))
        raise ValueError("Text must be at least 50 characters")
    summary = pipeline(
        text,
        max_length=50,
        min_length=10,
        do_sample=False,
        num_beams=4,
        length_penalty=1.0
    )[0]["summary_text"].strip()
    logger.debug("Summarized text: %s", summary)
    return summary
