import logging
from collections import Counter
import re

logger = logging.getLogger(__name__)

def keyword_density(text: str) -> dict:
    """Calculate keyword density of words in text."""
    try:
        if not text or not isinstance(text, str):
            logger.error("❌ Invalid text input: %s", text)
            raise ValueError("Text must be a non-empty string")
        
        # Clean and tokenize text
        words = re.findall(r'\b\w+\b', text.lower())
        if not words:
            logger.error("❌ No valid words found in text")
            raise ValueError("No valid words found")
        
        total_words = len(words)
        word_counts = Counter(words)
        density = {word: (count / total_words) * 100 for word, count in word_counts.items()}
        
        logger.debug("✅ Keyword density calculated: %s", density)
        return {"total_words": total_words, "density": density}
    except Exception as e:
        logger.exception("💥 Error calculating keyword density: %s", str(e))
        raise