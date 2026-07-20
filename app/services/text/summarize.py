import logging
from fastapi import HTTPException

logger = logging.getLogger(__name__)

class Summarizer:
    def __init__(self, app):
        self.app = app
        logger.debug("Text Summarizer initialized with preloaded t5-small")

    async def summarize_text(self, text: str) -> str:
        """
        Summarize input text using t5-small.
        Args:
            text (str): Input text to summarize.
        Returns:
            str: Summarized text.
        Raises:
            ValueError: If text is empty or too short.
        """
        if not text.strip():
            logger.error("Text is empty")
            raise ValueError("Text cannot be empty")
        if len(text) < 50:
            logger.error("Text too short: %d characters", len(text))
            raise ValueError("Text must be at least 50 characters")
        try:
            summarizer = self.app.state.summarize_pipeline
            summary = summarizer(
                text,
                max_length=50,
                min_length=10,
                do_sample=False,
                num_beams=4,
                length_penalty=1.0
            )[0]["summary_text"].strip()
            logger.debug("Summarized text: %s", summary)
            return summary
        except Exception as e:
            logger.error("Error summarizing text: %s", str(e))
            raise HTTPException(status_code=500, detail=f"Error summarizing text: {str(e)}")