from textblob import TextBlob
import logging

logger = logging.getLogger(__name__)

async def correct_grammar(text: str) -> dict:
    """
    Correct grammar in text using TextBlob with workaround for common errors.
    Args:
        text (str): Input text.
    Returns:
        dict: Original text and list of corrections.
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
        corrected_text = str(blob.correct())
        # Workaround for "This" -> "His" glitch
        if corrected_text.startswith("His ") and text.startswith("This "):
            corrected_text = "This " + corrected_text[4:]
        corrections = []
        if corrected_text != text:
            corrections.append({
                "original": text,
                "corrected": corrected_text
            })
        logger.debug("Grammar corrections: %s", corrections)
        return {"text": text, "corrections": corrections}
    except Exception as e:
        logger.exception("Error correcting grammar: %s", str(e))
        raise ValueError(f"Error correcting grammar: {str(e)}")