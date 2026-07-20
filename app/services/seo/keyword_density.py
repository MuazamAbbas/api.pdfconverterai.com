import re
from collections import Counter
import logging
from nltk.corpus import stopwords
import nltk

logger = logging.getLogger(__name__)

# Download NLTK stopwords
try:
    nltk.data.find('corpora/stopwords')
except LookupError:
    nltk.download('stopwords')
stop_words = set(stopwords.words('english'))

async def keyword_density(text: str) -> dict:
    """
    Analyze keyword density in the input text (excluding stopwords).
    Args:
        text (str): Input text.
    Returns:
        dict: Dictionary with keywords and their density (percentage).
    Raises:
        ValueError: If text is empty.
    """
    if not text:
        logger.error("Text is empty")
        raise ValueError("Text cannot be empty")
    words = re.findall(r'\b\w+\b', text.lower())
    if not words:
        logger.error("No words found in text")
        raise ValueError("No words found in text")
    
    # Filter out stopwords and short words
    filtered_words = [word for word in words if word not in stop_words and len(word) > 2]
    if not filtered_words:
        logger.error("No significant keywords found after filtering")
        raise ValueError("No significant keywords found")
    
    total_words = len(words)
    word_counts = Counter(filtered_words)
    
    # Calculate density as percentage
    density = {
        word: {"count": count, "density": round((count / total_words) * 100, 2)}
        for word, count in word_counts.items()
    }
    
    logger.debug("Keyword density: %s", density)
    return {"text": text, "keyword_density": density}