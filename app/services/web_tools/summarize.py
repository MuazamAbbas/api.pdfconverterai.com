import logging

import aiohttp
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)


async def fetch_webpage_text(url: str) -> str:
    """Fetch `url` and extract its `<p>` text, truncated to 1000 chars.

    Split out from the summarization step (unlike the prior
    `Summarizer.summarize_webpage`, which did fetch+extract+summarize in one
    method) so `app/services/web_tools/processors.py`'s
    `WebToolsSummarizeProcessor.execute()` can classify failures precisely
    (ADR-003): a network hiccup (`aiohttp.ClientError`) is transient/
    retryable, while no text extracted / text too short is a permanent,
    non-retryable input problem.

    Args:
        url (str): Webpage URL. Must start with `http://`/`https://`.

    Returns:
        str: Extracted `<p>` text, truncated to 1000 characters (unchanged
            from the prior implementation).

    Raises:
        ValueError: URL missing the http(s):// prefix, no text extracted,
            or extracted text is under 50 characters.
        aiohttp.ClientError: Network/HTTP failure fetching the page.
    """
    if not url.startswith(("http://", "https://")):
        logger.error("Invalid URL: %s", url)
        raise ValueError("URL must start with http:// or https://")

    logger.debug("Fetching URL: %s", url)
    async with aiohttp.ClientSession() as session:
        async with session.get(url, timeout=20) as response:
            response.raise_for_status()
            content = await response.text()

    soup = BeautifulSoup(content, "html.parser")
    paragraphs = soup.find_all("p")
    text = " ".join(p.get_text().strip() for p in paragraphs if p.get_text().strip())
    logger.debug("Extracted text length: %d", len(text))
    if not text:
        logger.error("No text extracted from URL: %s", url)
        raise ValueError("No text extracted from webpage")
    if len(text) < 50:
        logger.error("Text too short: %d characters", len(text))
        raise ValueError("Text must be at least 50 characters")
    return text[:1000]


async def summarize_webpage_text(text: str, pipeline) -> str:
    """Summarize already-extracted webpage text using a preloaded t5-small pipeline.

    Args:
        text (str): Extracted webpage text (see `fetch_webpage_text`).
        pipeline: Preloaded Hugging Face `summarization` pipeline
            (t5-small), loaded once per worker process in `app/worker.py`'s
            `on_startup` hook and passed in via `ctx["summarize_pipeline"]`
            (Handbook Part C.2/C.4, ADR-003/ADR-006) - the same shared
            pipeline `app/services/text/summarize.py`'s
            `summarize_text_service` uses, intentionally (previously reached
            via a `Summarizer(app)` instance in the FastAPI request
            process; that path is now Tier 2, so the pipeline lives in the
            ARQ worker process's `ctx` instead).

    Returns:
        str: Summarized text.
    """
    summary = pipeline(text, max_length=150, min_length=30, do_sample=False)[0]["summary_text"]
    logger.debug("Summary generated, length: %d", len(summary))
    return summary
