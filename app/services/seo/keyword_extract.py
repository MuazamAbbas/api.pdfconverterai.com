from keybert import KeyBERT
import logging

logger = logging.getLogger(__name__)
kw_model = KeyBERT(model="all-MiniLM-L6-v2")

async def extract_keywords_service(text: str) -> list:
    """
    Extract keywords from input text.
    Args:
        text (str): Text to analyze.
    Returns:
        list: List of extracted keywords.
    Raises:
        ValueError: If text is too short.
    """
    if len(text) < 20:
        logger.error("Text too short: %d characters", len(text))
        raise ValueError("Text must be at least 20 characters")
    try:
        keywords = kw_model.extract_keywords(text, keyphrase_ngram_range=(1, 2), top_n=5)
        logger.debug("Keywords extracted: %s", keywords)
        return [kw[0] for kw in keywords]
    except Exception as e:
        logger.exception("Error extracting keywords: %s", str(e))
        raise