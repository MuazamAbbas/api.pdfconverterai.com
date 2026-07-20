from textblob import TextBlob
import logging

logger = logging.getLogger(__name__)

async def analyze_sentiment_service(text: str) -> dict:
    """
    Analyze sentiment of text using TextBlob.
    Args:
        text (str): Input text.
    Returns:
        dict: Sentiment analysis result (polarity, subjectivity, label).
    Raises:
        ValueError: If text is empty or too short.
    """
    if not text:
        logger.error("Text is empty")
        raise ValueError("Text cannot be empty")
    if len(text) < 10:
        logger.error("Text too short: %d characters", len(text))
        raise ValueError("Text must be at least 10 characters")
    try:
        blob = TextBlob(text)
        polarity = blob.sentiment.polarity  # -1 (negative) to 1 (positive)
        subjectivity = blob.sentiment.subjectivity  # 0 (objective) to 1 (subjective)
        label = "positive" if polarity > 0 else "negative" if polarity < 0 else "neutral"
        result = {
            "text": text,
            "polarity": polarity,
            "subjectivity": subjectivity,
            "label": label
        }
        logger.debug("Sentiment analysis result: %s", result)
        return result
    except Exception as e:
        logger.exception("Error analyzing sentiment: %s", str(e))
        raise ValueError(f"Error analyzing sentiment: {str(e)}")