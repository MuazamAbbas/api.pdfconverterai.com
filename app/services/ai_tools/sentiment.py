from transformers import pipeline
import logging

logger = logging.getLogger(__name__)
sentiment_analyzer = pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english")

async def analyze_sentiment_service(text: str) -> dict:
    """
    Analyze sentiment of input text.
    Args:
        text (str): Text to analyze.
    Returns:
        dict: Sentiment label and confidence.
    Raises:
        ValueError: If text is too short.
    """
    if len(text) < 10:
        logger.error("Text too short: %d characters", len(text))
        raise ValueError("Text must be at least 10 characters")
    try:
        result = sentiment_analyzer(text)[0]
        logger.debug("Sentiment analysis result: %s", result)
        return {"sentiment": result["label"].lower(), "confidence": result["score"]}
    except Exception as e:
        logger.exception("Error analyzing sentiment: %s", str(e))
        raise