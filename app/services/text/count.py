import re
import logging

logger = logging.getLogger(__name__)

async def word_count(text: str) -> int:
    """
    Count words in the input text.
    Args:
        text (str): Input text.
    Returns:
        int: Number of words.
    Raises:
        ValueError: If text is empty.
    """
    if not text:
        logger.error("Text is empty")
        raise ValueError("Text cannot be empty")
    words = re.findall(r'\b\w+\b', text.lower())
    count = len(words)
    logger.debug("Word count for text: %d", count)
    return count

async def char_count(text: str) -> int:
    """
    Count characters in the input text.
    Args:
        text (str): Input text.
    Returns:
        int: Number of characters.
    Raises:
        ValueError: If text is empty.
    """
    if not text:
        logger.error("Text is empty")
        raise ValueError("Text cannot be empty")
    count = len(text)
    logger.debug("Character count for text: %d", count)
    return count

async def sentence_count(text: str) -> int:
    """
    Count sentences in the input text.
    Args:
        text (str): Input text.
    Returns:
        int: Number of sentences.
    Raises:
        ValueError: If text is empty.
    """
    if not text:
        logger.error("Text is empty")
        raise ValueError("Text cannot be empty")
    sentences = re.split(r'[.!?]+', text)
    sentences = [s.strip() for s in sentences if s.strip()]
    count = len(sentences)
    logger.debug("Sentence count for text: %d", count)
    return count

async def paragraph_count(text: str) -> int:
    """
    Count paragraphs in the input text.
    Args:
        text (str): Input text.
    Returns:
        int: Number of paragraphs.
    Raises:
        ValueError: If text is empty.
    """
    if not text:
        logger.error("Text is empty")
        raise ValueError("Text cannot be empty")
    paragraphs = [p.strip() for p in text.split('\n') if p.strip()]
    count = len(paragraphs)
    logger.debug("Paragraph count for text: %d", count)
    return count