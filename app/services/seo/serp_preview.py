import logging

logger = logging.getLogger(__name__)

# Google's SERP snippet widget typically displays roughly the first ~60
# characters of a title and ~160 characters of a meta description before
# truncating (character-count approximation, not pixel-width - same
# convention used by title_length_checker.py). This endpoint only flags
# whether the raw input exceeds those thresholds; the actual
# truncated-preview rendering happens frontend-side, not here.
_TITLE_DISPLAY_LIMIT = 60
_DESCRIPTION_DISPLAY_LIMIT = 160


async def analyze_serp_preview(title: str, description: str, url: str) -> dict:
    """
    Return truncation-risk metadata for a SERP preview, without applying
    any truncation server-side.

    Args:
        title (str): Page title.
        description (str): Page meta description.
        url (str): Page URL.
    Returns:
        dict: raw field values plus length counts and boolean
            exceeds-limit flags for title/description.
    Raises:
        ValueError: If title, description, or url is empty/whitespace-only.
    """
    if not title or not title.strip():
        logger.error("Title is empty")
        raise ValueError("Title cannot be empty")
    if not description or not description.strip():
        logger.error("Description is empty")
        raise ValueError("Description cannot be empty")
    if not url or not url.strip():
        logger.error("URL is empty")
        raise ValueError("URL cannot be empty")

    result = {
        "title": title,
        "description": description,
        "url": url,
        "title_length": len(title),
        "description_length": len(description),
        "title_exceeds_limit": len(title) > _TITLE_DISPLAY_LIMIT,
        "description_exceeds_limit": len(description) > _DESCRIPTION_DISPLAY_LIMIT,
    }
    logger.debug("SERP preview result: %s", result)
    return result
