import logging
import aiohttp
from bs4 import BeautifulSoup
from fastapi import HTTPException

logger = logging.getLogger(__name__)

class Summarizer:
    def __init__(self, app):
        self.app = app
        logger.debug("Summarizer initialized with preloaded t5-small")

    async def summarize_webpage(self, url: str) -> str:
        """
        Fetch and summarize text from a webpage.
        Args:
            url (str): Webpage URL.
        Returns:
            str: Summarized text.
        Raises:
            ValueError: If URL is invalid or no text is extracted.
        """
        if not url.startswith(("http://", "https://")):
            logger.error("Invalid URL: %s", url)
            raise ValueError("URL must start with http:// or https://")
        try:
            logger.debug("Fetching URL: %s", url)
            async with aiohttp.ClientSession() as session:
                async with session.get(url, timeout=20) as response:
                    response.raise_for_status()
                    content = await response.text()
            soup = BeautifulSoup(content, "html.parser")
            paragraphs = soup.find_all("p")
            text = " ".join(p.get_text().strip() for p in paragraphs if p.get_text().strip())
            logger.debug("Extracted text length: %d", len(text))
            if not text:
                logger.error("No text extracted from URL: %s", url)
                raise ValueError("No text extracted from webpage")
            if len(text) < 50:
                logger.error("Text too short: %d characters", len(text))
                raise ValueError("Text must be at least 50 characters")
            text = text[:1000]
            summarizer = self.app.state.summarize_pipeline
            summary = summarizer(
                text,
                max_length=150,
                min_length=30,
                do_sample=False
            )[0]["summary_text"]
            logger.debug("Summary generated, length: %d", len(summary))
            return summary
        except aiohttp.ClientError as e:
            logger.error("Error fetching webpage: %s", str(e))
            raise ValueError(f"Error fetching webpage: {str(e)}")
        except Exception as e:
            logger.error("Error summarizing webpage: %s", str(e))
            raise HTTPException(status_code=500, detail=f"Error summarizing webpage: {str(e)}")

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