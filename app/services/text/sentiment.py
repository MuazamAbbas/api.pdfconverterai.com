import logging
from textblob import TextBlob
from fastapi import HTTPException

logger = logging.getLogger(__name__)

class SentimentAnalyzer:
    async def analyze(self, text: str) -> dict:
        """
        Analyze sentiment of input text using TextBlob.
        Args:
            text (str): Input text to analyze.
        Returns:
            dict: Sentiment analysis results (sentiment label, polarity, subjectivity).
        Raises:
            HTTPException: If text is empty or analysis fails.
        """
        try:
            if not text.strip():
                logger.error("Empty text provided for sentiment analysis")
                raise HTTPException(status_code=400, detail="Text cannot be empty")
            blob = TextBlob(text)
            polarity = blob.sentiment.polarity
            subjectivity = blob.sentiment.subjectivity
            sentiment = "positive" if polarity > 0 else "negative" if polarity < 0 else "neutral"
            logger.debug("Sentiment analysis: polarity=%f, subjectivity=%f, sentiment=%s", polarity, subjectivity, sentiment)
            return {
                "sentiment": sentiment,
                "polarity": polarity,
                "subjectivity": subjectivity
            }
        except Exception as e:
            logger.error("Error in sentiment analysis: %s", str(e))
            raise HTTPException(status_code=500, detail=f"Error in sentiment analysis: {str(e)}")