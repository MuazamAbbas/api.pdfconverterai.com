from transformers import pipeline
import numpy as np
import logging

logger = logging.getLogger(__name__)
sentiment_analyzer = pipeline("sentiment-analysis", model="distilbert-base-uncased-finetuned-sst-2-english")

async def calculate_financial_plan(principal: float, rate: float, years: int, market_sentiment: str) -> dict:
    """
    Calculate investment growth with sentiment-based adjustment.
    Args:
        principal (float): Initial investment.
        rate (float): Annual interest rate (%).
        years (int): Investment duration (years).
        market_sentiment (str): Market sentiment text.
    Returns:
        dict: Investment and adjusted values.
    Raises:
        ValueError: If inputs are invalid.
    """
    if principal <= 0 or rate < 0 or years <= 0:
        logger.error("Invalid inputs: principal=%f, rate=%f, years=%d", principal, rate, years)
        raise ValueError("Principal, rate, and years must be positive")
    if not market_sentiment:
        logger.error("Market sentiment is empty")
        raise ValueError("Market sentiment cannot be empty")
    try:
        # Compound interest
        amount = principal * (1 + rate / 100) ** years
        # Sentiment adjustment
        sentiment = sentiment_analyzer(market_sentiment)[0]
        adjustment = 1.05 if sentiment["label"].lower() == "positive" else 0.95 if sentiment["label"].lower() == "negative" else 1.0
        adjusted_amount = amount * adjustment
        logger.debug("Financial plan calculated: amount=%f, adjusted=%f, sentiment=%s", amount, adjusted_amount, sentiment["label"])
        return {
            "investment_value": round(amount, 2),
            "sentiment_adjusted_value": round(adjusted_amount, 2),
            "sentiment": sentiment["label"].lower()
        }
    except Exception as e:
        logger.exception("Error calculating financial plan: %s", str(e))
        raise