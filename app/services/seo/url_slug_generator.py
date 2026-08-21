import logging
import re
import unicodedata

logger = logging.getLogger(__name__)


async def generate_slug(text: str) -> dict:
    """
    Convert arbitrary text into a URL-safe slug: lowercase, accented
    characters transliterated to ASCII where possible, any run of
    non-alphanumeric characters collapsed to a single hyphen, and no
    leading/trailing hyphens.

    Args:
        text (str): Input text to slugify.
    Returns:
        dict: original text and the generated slug.
    Raises:
        ValueError: If text is empty/whitespace-only, or contains no
            characters that can form a slug (e.g. only punctuation/emoji).
    """
    if not text or not text.strip():
        logger.error("Text is empty")
        raise ValueError("Text cannot be empty")

    # Normalize unicode (e.g. accented chars) to closest ASCII equivalent
    # before slugifying, so "Café" -> "cafe" instead of being stripped down
    # to "caf".
    normalized = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    lowered = normalized.lower()

    # Collapse any run of non-alphanumeric characters into a single hyphen,
    # which also handles "no double hyphens" for free.
    slug = re.sub(r"[^a-z0-9]+", "-", lowered)
    slug = slug.strip("-")

    if not slug:
        logger.error("No sluggable characters found in text")
        raise ValueError("Text does not contain any characters that can form a slug")

    result = {"original": text, "slug": slug}
    logger.debug("Slug generated: %s", result)
    return result
