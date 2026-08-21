import logging

logger = logging.getLogger(__name__)


async def check_title_length(title: str) -> dict:
    """
    Evaluate a page `<title>` length against Google's typical SERP display
    guidance.

    Thresholds (character-count approximation, not pixel-width - Google's
    actual cutoff is pixel-based and varies by character widths, but a
    character count is the standard approximation most SEO tools use):
      - < 30 chars: "too_short" - likely under-descriptive, wastes SERP
        real estate that could carry more keywords/context.
      - 30-60 chars: "good" - Google's SERP snippet widget typically shows
        roughly 50-60 characters of title before truncating on desktop;
        30 is a practical floor for a meaningful title.
      - > 60 chars: "too_long" - meaningful risk of truncation (the actual
        cutoff can drift up toward ~70 chars depending on character
        widths, but 60 is the safe upper bound most SEO guidance uses).

    Args:
        title (str): Page title to evaluate.
    Returns:
        dict: title, character length, word count, and a status flag.
    Raises:
        ValueError: If title is empty/whitespace-only.
    """
    if not title or not title.strip():
        logger.error("Title is empty")
        raise ValueError("Title cannot be empty")

    length = len(title)
    word_count = len(title.split())

    if length < 30:
        status = "too_short"
    elif length <= 60:
        status = "good"
    else:
        status = "too_long"

    result = {
        "title": title,
        "length": length,
        "word_count": word_count,
        "status": status,
    }
    logger.debug("Title length check result: %s", result)
    return result
