import logging

import nltk
from rake_nltk import Rake

logger = logging.getLogger(__name__)

# Rake() tokenizes with nltk's word_tokenize/sent_tokenize under the hood,
# which need both 'punkt' and 'punkt_tab' (nltk >=3.8.2 split the data
# between the two) downloaded - not guaranteed by import order alone since
# this module doesn't depend on app.services.unit_converters.contextual_convert,
# the other place these get downloaded.
nltk.download('punkt', quiet=True)
nltk.download('punkt_tab', quiet=True)

r = Rake()

async def extract_keywords_service(text: str) -> list:
    """
    Extract keywords from input text using RAKE.
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
        r.extract_keywords_from_text(text)
        keywords = r.get_ranked_phrases()[:5]
        logger.debug("Keywords extracted: %s", keywords)
        return keywords
    except Exception as e:
        logger.exception("Error extracting keywords: %s", str(e))
        raise